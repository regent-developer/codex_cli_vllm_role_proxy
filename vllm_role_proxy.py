"""
本地代理，模拟 Ollama 服务器，同时将请求转发到远程 vLLM 服务器。
这使得 `codex --oss --local-provider ollama` 能够与任何由 vLLM 提供服务的模型协同工作。

解决了两个问题：
1. Codex CLI 硬编码了 “developer” 消息角色；vLLM 会返回 HTTP 400 “Unexpected message role” 拒绝该请求。
   该代理将 developer 重写为 system，并将顶层的 `instructions` 合并到第一条 system 消息中。
2. Codex 内置的 ollama 提供程序会探测 Ollama 原生端点（/api/version、/api/tags），而 vLLM 没有这些端点。
   本代理对这些端点进行了桩（stub）处理，使 Codex 的启动检查能够通过。

用法：
    python vllm_role_proxy.py [--listen 127.0.0.1:11434] \\
        [--target http://192.168.0.10:8000] [--vllm-model MODEL]

然后使用内置的 ollama 提供程序运行 Codex：
    codex --oss                                    # 交互式 TUI
    codex --oss exec --skip-git-repo-check "..."   # 非交互式

Codex CLI v0.143+ 要求 wire_api = "responses"（chat 已不再支持）。
本代理将 /v1/responses 直接转发给 vLLM（vLLM 0.22+ 支持该接口）。

作者: 匈奴牛逼县令
创建日期: 2026-07-12
版本: 1.0
"""

import argparse
import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("vllm-proxy")

# Codex 接受的最低 Ollama 版本（来自 codex-rs/ollama/src/lib.rs）。
OLLAMA_STUB_VERSION = "0.13.4"


def _extract_text(content: Any) -> str:
    """从消息的 content 字段（字符串或部件列表）中提取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    return ""


def rewrite_role(body: dict) -> dict:
    """将所有 “developer” 角色重写为 “system”，并将所有 system 消息合并为一条放在最前面。

    vLLM 会拒绝：
    - “developer” 角色：HTTP 400 “Unexpected message role”
    - 不在位置 0 的 system 消息：“System message must be at the beginning”

    此函数：
    1. 在所有位置（包括嵌套项）将 developer 重写为 system。
    2. 将顶层的 `instructions` 字段（Responses API）合并到 system 消息中。
    3. 将所有 system 消息合并为一条并置于位置 0，移除出现在对话中间（例如，后期轮次中由 developer 重写为 system 的消息）的 system 消息。
    """
    if not isinstance(body, dict):
        return body

    def fix_item(item: Any) -> Any:
        if isinstance(item, dict):
            if item.get("role") == "developer":
                item = {**item, "role": "system"}
            return {k: fix_item(v) for k, v in item.items()}
        if isinstance(item, list):
            return [fix_item(x) for x in item]
        return item

    for key in ("messages", "input"):
        if key in body:
            body[key] = fix_item(body[key])

    # 弹出顶层的 instructions（Responses API）—— vLLM 会将其转换为单独的 system 消息，
    # 导致 “System message must be at the beginning” 错误。
    instructions = body.pop("instructions", None)

    # 对 “input”（Responses）和 “messages”（Chat）两种键进行 system 消息合并。
    for key in ("input", "messages"):
        items = body.get(key)
        if not isinstance(items, list):
            continue

        system_texts = []
        # 仅对 “input” 键（Responses API）将顶层的 instructions 前置。
        if instructions and key == "input":
            system_texts.append(instructions)

        other_items = []
        for item in items:
            if isinstance(item, dict) and item.get("role") == "system":
                text = _extract_text(item.get("content"))
                if text:
                    system_texts.append(text)
            else:
                other_items.append(item)

        if system_texts:
            if key == "input":
                merged_system = {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": "\n\n".join(system_texts)}],
                }
            else:  # messages（chat completions）
                merged_system = {
                    "role": "system",
                    "content": "\n\n".join(system_texts),
                }
            body[key] = [merged_system] + other_items

    return body


def remap_model(body: dict, vllm_model: str) -> dict:
    """将请求体中的 `model` 字段重映射为 vLLM 模型名称。

    Codex 的 --oss 模式默认使用 “gpt-oss:20b”；vLLM 无法识别该名称。
    此函数确保将所有传入的模型名称替换为实际的 vLLM 名称。
    """
    if isinstance(body, dict) and "model" in body:
        body["model"] = vllm_model
    return body


async def discover_vllm_model(target_base: str) -> Optional[str]:
    """查询 vLLM 的 /v1/models 以发现第一个可用的模型名称。"""
    url = f"{target_base}/v1/models"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.warning("vLLM /v1/models 返回状态码 %d", resp.status)
                    return None
                data = await resp.json()
                models = data.get("data", [])
                if models and isinstance(models, list):
                    name = models[0].get("id")
                    if name:
                        return name
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("发现 vLLM 模型失败：%s", exc)
    return None


# ---------------------------------------------------------------------------
# Ollama 原生端点桩（stub）
# ---------------------------------------------------------------------------

async def handle_api_version(request: web.Request) -> web.Response:
    """桩处理 GET /api/version —— Codex 检查版本是否 >= 0.13.4。"""
    return web.json_response({"version": OLLAMA_STUB_VERSION})


async def handle_api_tags(request: web.Request) -> web.Response:
    """桩处理 GET /api/tags —— 列出 vLLM 模型，使 Codex 跳过 /api/pull。"""
    vllm_model = request.app["vllm_model"]
    return web.json_response({
        "models": [
            {
                "name": vllm_model,
                "model": vllm_model,
                "modified_at": "2026-01-01T00:00:00Z",
                "size": 0,
                "digest": "sha256:0",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "qwen",
                    "families": ["qwen"],
                    "parameter_size": "35B",
                    "quantization_level": "Q4_0",
                },
            }
        ]
    })


async def handle_api_pull(request: web.Request) -> web.StreamResponse:
    """桩处理 POST /api/pull —— 伪造一个成功的拉取流（备用）。"""
    resp = web.StreamResponse(
        status=200,
        headers={"Content-Type": "application/x-ndjson"},
    )
    await resp.prepare(request)
    await resp.write(json.dumps({"status": "pulling manifest"}).encode() + b"\n")
    await resp.write(json.dumps({"status": "success"}).encode() + b"\n")
    await resp.write_eof()
    return resp


# ---------------------------------------------------------------------------
# 全部捕获代理（将 /v1/* 转发给 vLLM）
# ---------------------------------------------------------------------------

async def proxy(request: web.Request) -> web.StreamResponse:
    """将请求转发给 vLLM，同时重写角色并重映射模型名称。"""
    target_base = request.app["target_base"]
    vllm_model = request.app["vllm_model"]
    path = request.match_info.get("tail", "")
    # 重构上游 URL，包含查询字符串。
    upstream_url = f"{target_base}/{path}"
    if request.query_string:
        upstream_url += f"?{request.query_string}"

    method = request.method
    raw_body = await request.read()

    # 对 JSON POST/PATCH 请求体进行角色重写和模型重映射。
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    if method in ("POST", "PATCH", "PUT") and raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                parsed = rewrite_role(parsed)
                if vllm_model:
                    parsed = remap_model(parsed, vllm_model)
                raw_body = json.dumps(parsed).encode()
                # 更新 Content-Length 为改写后的请求体大小。
                headers["Content-Length"] = str(len(raw_body))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # 非 JSON，原样转发。
            pass

    log.info("%s %s -> %s", method, path, upstream_url)

    session: aiohttp.ClientSession = request.app["client"]
    try:
        upstream = await session.request(
            method,
            upstream_url,
            headers=headers,
            data=raw_body if raw_body else None,
            allow_redirects=False,
        )
    except aiohttp.ClientError as exc:
        log.error("上游连接失败：%s", exc)
        return web.Response(status=502, text=f"upstream error: {exc}")

    # 将上游响应流式传回客户端。
    resp = web.StreamResponse(
        status=upstream.status,
        reason=upstream.reason,
    )
    # 转发除逐跳头部以外的头部。
    for k, v in upstream.headers.items():
        if k.lower() in ("transfer-encoding", "content-encoding", "content-length", "connection"):
            continue
        resp.headers[k] = v

    await resp.prepare(request)
    try:
        async for chunk in upstream.content.iter_any():
            if chunk:
                await resp.write(chunk)
        await resp.write_eof()
    finally:
        upstream.release()
    return resp


async def on_startup(app: web.Application) -> None:
    app["client"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None),
    )


async def on_cleanup(app: web.Application) -> None:
    session: aiohttp.ClientSession = app["client"]
    await session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="用于 Codex CLI 的 vLLM Ollama 桥接代理")
    parser.add_argument("--listen", default="127.0.0.1:11434", help="监听地址（host:port）")
    parser.add_argument(
        "--target",
        default="http://10.41.0.98:8000",
        help="上游 vLLM 基础 URL（不带末尾 /v1）",
    )
    parser.add_argument(
        "--vllm-model",
        default=None,
        help="vLLM 模型名称（若未指定则自动发现）",
    )
    args = parser.parse_args()

    target_base = args.target.rstrip("/")
    host, _, port_str = args.listen.rpartition(":")
    port = int(port_str) if port_str else 11434

    # 发现 vLLM 模型名称（阻塞操作 — 在事件循环启动前执行）。
    vllm_model = args.vllm_model
    if vllm_model is None:
        log.info("正在从 %s/v1/models 发现 vLLM 模型 ...", target_base)
        vllm_model = asyncio.run(discover_vllm_model(target_base))
        if vllm_model is None:
            log.error("无法发现 vLLM 模型；请使用 --vllm-model 指定")
            raise SystemExit(1)
        log.info("vLLM 模型：%s（自动发现）", vllm_model)
    else:
        log.info("vLLM 模型：%s（指定）", vllm_model)

    app = web.Application(client_max_size=100 * 1024 * 1024)
    app["target_base"] = target_base
    app["vllm_model"] = vllm_model
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # 先注册 Ollama 原生桩，使其优先于全部捕获路由。
    app.router.add_get("/api/version", handle_api_version)
    app.router.add_get("/api/tags", handle_api_tags)
    app.router.add_post("/api/pull", handle_api_pull)

    # 全部捕获：将其余所有请求（包括 /v1/*）转发给 vLLM。
    app.router.add_route("*", "/{tail:.*}", proxy)

    log.info("vLLM Ollama 桥接代理：http://%s:%d -> %s", host or "127.0.0.1", port, target_base)
    log.info("用法：")
    log.info("  codex --oss                                    # 交互式 TUI")
    log.info("  codex --oss exec --skip-git-repo-check \"...\"   # 非交互式")
    web.run_app(app, host=host or "127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()