<!--
文档元信息
作者：KO
创建时间：2026-07-10
最后更新：2026-07-10
版本：v1.0
-->
# Codex CLI 接入本地 vLLM 大模型使用指南
> **作者**：KO  
> **创建时间**：2026-07-10  
> **最后更新**：2026-07-10  
> **版本**：v1.0  

本文档介绍如何通过中间代理让 OpenAI Codex CLI 接入同网段启动的 vLLM 大模型服务。

## 背景与问题

**Codex CLI**（`@openai/codex`，v0.143+）默认连接 OpenAI 云端 API。接入本地 vLLM 时存在两个兼容性问题：

| 问题 | 原因 | vLLM 报错 |
|------|------|-----------|
| 消息角色不兼容 | Codex 硬编码使用 `developer` 角色发送系统指令，vLLM 只接受 `system`/`user`/`assistant`/`tool` | `400 "Unexpected message role"` |
| System 消息位置错误 | Codex 同时发送顶层 `instructions` 字段和 `input` 数组中的 system 消息，vLLM 要求 system 消息只能在开头 | `400 "System message must be at the beginning"` |
| Ollama 原生端点缺失 | `codex --oss` 启动时探测 `/api/version`、`/api/tags` 等 Ollama 原生端点，vLLM 没有这些端点 | 启动失败 |

Codex CLI **没有配置选项**可以更改消息角色（已通过源码研究和 GitHub Issue #9612 确认），因此必须使用中间代理进行转换。

## 方案概览

```
Codex CLI  ──►  本地代理 (vllm_role_proxy.py)  ──►  vLLM 服务器
              重写 developer→system              (同网段)
              合并 system 消息到开头
              模拟 Ollama 端点
              模型名重映射
```

代理监听 `127.0.0.1:11434`（与 Ollama 默认端口一致），将请求转发到 vLLM，同时：
- 将 `developer` 角色重写为 `system`
- 合并所有 system 消息到数组开头
- 模拟 Ollama 原生端点（`/api/version`、`/api/tags`、`/api/pull`）
- 自动发现 vLLM 模型名并重映射请求中的 `model` 字段

## 前置条件

- **Node.js** v20+ 已安装（Codex CLI 依赖）
- **vLLM** 服务已在同网段启动，且开启了 Responses API 支持
- **Python 3.10+** 已安装
- Python 依赖：`aiohttp`（`pip install aiohttp`）

## 安装 Codex CLI

### 1. 安装 Node.js

如已安装 Node.js v20+，可跳过此步。

Windows 下推荐使用 [nvm-windows](https://github.com/coreybutler/nvm-windows) 管理多版本 Node.js：

```powershell
# 使用 winget 安装 nvm-windows
winget install CoreyButler.NVMforWindows

# 重启终端后安装 Node.js LTS
nvm install lts
nvm use lts

# 验证
node --version   # 应 >= v20
npm --version
```

也可直接从 [Node.js 官网](https://nodejs.org/) 下载 LTS 安装包。

### 2. 安装 Codex CLI

通过 npm 全局安装：

```powershell
npm install -g @openai/codex

# 验证安装
codex --version
# 预期输出：codex-cli 0.143.0
```

**说明：**
- Codex CLI 是 Rust 编译的二进制，npm 包会根据平台自动下载对应预编译版本
- 安装后 `codex` 命令全局可用
- 升级：`npm update -g @openai/codex`

### 3. 安装 Python 依赖

代理脚本依赖 `aiohttp`：

```powershell
pip install aiohttp
```

## 配置 `.codex` 目录

Codex CLI 使用 `CODEX_HOME` 环境变量或默认目录 `~/.codex`（Windows 下为 `C:\Users\<用户名>\.codex`）存放配置。本文档采用项目内 `.codex` 目录，便于管理。

### 1. 创建 `.codex` 目录结构

```powershell
# 在项目根目录下创建
mkdir d:\codex\.codex
```

完整目录结构（运行后自动生成大部分子目录）：

```
d:\codex\
├── .codex\
│   ├── config.toml          # 主配置文件（手动创建）
│   ├── sessions\            # 会话记录（自动生成）
│   ├── rules\               # 沙箱规则（自动生成）
│   └── .sandbox\            # 沙箱状态（自动生成）
└── vllm_role_proxy.py       # 代理脚本
```

### 2. 配置 `config.toml`

创建 `d:\codex\.codex\config.toml`，内容如下：

```toml
# ===== 模型配置 =====
model = "Qwen3.6-35B-A3B"              # 必须与 vLLM 启动时的模型名一致
model_provider = "vllm_shim"           # 使用自定义 provider
api_key = "dummy"                      # vLLM 不校验 key，随便填

# ===== 自定义 Provider 配置 =====
[model_providers.vllm_shim]
name = "vLLM Local (role-rewrite proxy)"
base_url = "http://127.0.0.1:11434/v1" # 指向本地代理（不是 vLLM 直连）
wire_api = "responses"                 # v0.143+ 必须用 responses，chat 已废弃
requires_openai_auth = false           # 不需要 OpenAI 认证

# ===== 项目信任级别（可选）=====
[projects.'c:\users\user']
trust_level = "trusted"                # 避免每次启动询问信任

# ===== Windows 沙箱配置（可选）=====
[windows]
sandbox = "elevated"                   # Windows 下沙箱模式
```

### 3. 配置字段详解

#### 顶层字段

| 字段 | 说明 | 示例值 |
|------|------|--------|
| `model` | 模型名称，必须与 vLLM 启动时的 `--served-model-name` 一致 | `"Qwen3.6-35B-A3B"` |
| `model_provider` | 使用的 provider id，对应 `[model_providers.xxx]` 的 key | `"vllm_shim"` |
| `api_key` | API 密钥，vLLM 不校验，填 `"dummy"` 即可 | `"dummy"` |

#### `[model_providers.vllm_shim]` 字段

| 字段 | 说明 | 示例值 |
|------|------|--------|
| `name` | provider 显示名称 | `"vLLM Local"` |
| `base_url` | API 基础地址，**指向代理而非 vLLM 直连** | `"http://127.0.0.1:11434/v1"` |
| `wire_api` | 传输协议，v0.143+ 只支持 `"responses"` | `"responses"` |
| `requires_openai_auth` | 是否需要 OpenAI 认证，vLLM 不需要 | `false` |

**重要：**
- `base_url` 必须指向代理（`127.0.0.1:11434`），不能直连 vLLM，否则会因 `developer` 角色被拒绝
- `wire_api` 只能是 `"responses"`，`"chat"` 在 v0.143+ 已废弃会报错
- `--oss` 方式（见下文）**不需要**配置 `model_provider`，内置 ollama provider 会自动连接代理

### 4. 设置 `CODEX_HOME` 环境变量

自定义 provider 方式需要让 codex 读取项目的 `.codex` 目录：

```powershell
# 临时设置（当前会话有效）
$env:CODEX_HOME = "d:\codex\.codex"

# 永久设置（用户级）
[nvironmnt]::SetEnvironmentVariable("CODEX_HOME", "d:\codex\.codex", "User")
```

验证：

```powershell
echo $env:CODEX_HOME
# 预期输出：d:\codex\.codex
```

**说明：**
- `--oss` 方式不需要设置 `CODEX_HOME`，内置 provider 忽略 `model_provider` 配置
- 自定义 provider 方式必须设置，否则 codex 会读取默认的 `C:\Users\<用户名>\.codex\config.toml`

## 文件说明

本方案涉及两个文件（均位于 `d:\codex\`）：

| 文件 | 作用 |
|------|------|
| `vllm_role_proxy.py` | 核心代理脚本，模拟 Ollama + 转发 vLLM |
| `.codex/config.toml` | Codex CLI 配置（仅自定义 provider 方式需要） |

## 使用方法

### 方式：`codex --oss`（推荐，最简单）

利用 Codex 内置的 Ollama provider，代理模拟 Ollama 服务器。

#### 1. 启动代理

```powershell
python d:\codex\vllm_role_proxy.py --listen 127.0.0.1:11434 --target http://192.168.0.10:8000
```

**参数说明：**
- `--listen`：代理监听地址（默认 `127.0.0.1:11434`，与 Ollama 默认端口一致）
- `--target`：vLLM 服务器地址（**不含** `/v1` 后缀）
- `--vllm-model`：vLLM 模型名（可选，省略则自动从 `/v1/models` 发现）

启动成功后会显示：
```
[INFO] vLLM model: Qwen3.6-35B-A3B (auto-discovered)
[INFO] vLLM Ollama-shim proxy: http://127.0.0.1:11434 -> http://192.168.0.10:8000
```

#### 2. 运行 Codex CLI

```powershell
# 交互式 TUI
codex --oss --local-provider ollama

# 非交互式（单次推理）
codex --oss --local-provider ollama exec --skip-git-repo-check "你的问题"

# 非交互式 + 只读沙箱（推荐用于测试）
codex --oss --local-provider ollama exec --skip-git-repo-check --sandbox read-only --ephemeral "What is 17 + 28?"
```

**说明：**
- `--oss`：使用本地开源模型 provider
- `--local-provider ollama`：指定使用 Ollama provider（连接 `localhost:11434`）
- 无需设置 `CODEX_HOME` 环境变量，无需修改任何配置文件
- 模型名无需指定，代理会自动重映射为 vLLM 实际模型名

#### 3. 验证测试

```powershell
codex --oss --local-provider ollama exec --skip-git-repo-check --sandbox read-only --ephemeral "What is 17 + 28? Reply with only the number."
```

预期输出：`45`，退出码 0。

---

## 代理工作原理

### 路由处理

代理按以下优先级处理请求：

| 路由 | 处理方式 | 说明 |
|------|----------|------|
| `GET /api/version` | 返回 stub | 返回 `{"version":"0.13.4"}`，通过 codex 版本门控 |
| `GET /api/tags` | 返回 stub | 返回 vLLM 模型名，让 codex 认为模型已存在，跳过下载 |
| `POST /api/pull` | 返回 stub | 返回成功流（兜底，正常不触发） |
| `* /{tail:.*}` | 转发到 vLLM | 处理 `/v1/models`、`/v1/responses` 等 OpenAI 兼容端点 |

### 请求改写

对 `POST /v1/responses` 和 `POST /v1/chat/completions` 请求体进行以下改写：

1. **角色重写**：将所有 `developer` 角色改为 `system`
2. **System 消息合并**：收集数组中所有 system 消息，合并文本后放在数组位置 0
3. **Instructions 合并**：将顶层 `instructions` 字段（Responses API）合并到 system 消息中，并删除该字段
4. **模型名重映射**：将 `model` 字段替换为 vLLM 实际模型名

### `codex --oss` 启动流程

代理模拟了 codex 启动时的完整探测序列：

```
1. GET /v1/models        → 转发到 vLLM（存活探测）
2. GET /api/version      → 返回 {"version":"0.13.4"}（版本门控）
3. GET /api/tags         → 返回 {"models":[{"name":"Qwen3.6-35B-A3B"}]}（模型列表）
4. POST /v1/responses    → 转发到 vLLM（实际推理，SSE 流式）
```

## 常用命令速查

```powershell
# ========== 代理管理 ==========

# 启动代理
python d:\personal\codex\vllm_role_proxy.py --listen 127.0.0.1:11434 --target http://10.41.0.98:8000

# 查看端口占用
Get-NetTCPConnection -LocalPort 11434

# 停止占用 11434 端口的进程
$ports = Get-NetTCPConnection -LocalPort 11434; $ports | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }

# ========== Codex CLI（--oss 方式） ==========

# 交互式 TUI
codex --oss --local-provider ollama

# 非交互式推理
codex --oss --local-provider ollama exec --skip-git-repo-check "你的问题"

# 非交互式 + 只读沙箱
codex --oss --local-provider ollama exec --skip-git-repo-check --sandbox read-only --ephemeral "你的问题"

# ========== Codex CLI（自定义 provider 方式） ==========

# 设置配置目录
$env:CODEX_HOME = "d:\personal\codex\.codex"

# 交互式
codex

# 非交互式
codex exec --skip-git-repo-check --sandbox read-only --ephemeral "你的问题"

# ========== 测试代理端点 ==========

# 测试 Ollama 模拟端点
curl.exe -s http://127.0.0.1:11434/api/version
curl.exe -s http://127.0.0.1:11434/api/tags

# 测试 vLLM 转发
curl.exe -s http://127.0.0.1:11434/v1/models
```

## 常见问题

### 1. `error while attempting to bind on address ('127.0.0.1', 11434)`

端口 11434 被占用。停止占用进程后重启代理：

```powershell
$ports = Get-NetTCPConnection -LocalPort 11434; $ports | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### 2. `"Unexpected message role"`

vLLM 收到了 `developer` 角色。确认代理正在运行，且请求经过代理（`base_url` 指向 `127.0.0.1:11434` 而非 vLLM 直连）。

### 3. `"System message must be at the beginning"`

多轮对话中 system 消息出现在数组中间。代理已修复此问题（合并所有 system 消息到位置 0），请确保使用最新版 `vllm_role_proxy.py`。

### 4. `No default OSS provider configured`

未指定 `--local-provider`。使用 `codex --oss --local-provider ollama`（不能省略 `--local-provider ollama`）。

### 5. `wire_api = "chat" is no longer supported`

Codex CLI v0.143+ 不再支持 `wire_api = "chat"`，必须使用 `wire_api = "responses"`。代理已默认使用 responses API。

### 6. 代理启动时报 `could not discover vLLM model`

vLLM 服务器不可达或未启动。检查：
- vLLM 服务是否正常运行：`curl.exe -s http://10.41.0.98:8000/v1/models`
- `--target` 参数是否正确（不含 `/v1` 后缀）
- 网络是否可达

也可以手动指定模型名：`--vllm-model Qwen3.6-35B-A3B`

## 环境信息

- **Codex CLI 版本**：v0.143.0
- **Node.js 版本**：v20+（实测 v24.13.0）
- **npm 版本**：v10+（实测 v11.6.2）
- **vLLM 版本**：0.22.1+（需支持 `/v1/responses` 端点）
- **代理依赖**：Python 3.10+，aiohttp
- **操作系统**：Windows（PowerShell 命令）
- **默认端口**：11434（代理监听）
- **vLLM 地址**：`http://192.168.0.10:8000`（示例，按实际替换）
- **模型名**：`Qwen3.6-35B-A3B`（示例，按实际替换）
