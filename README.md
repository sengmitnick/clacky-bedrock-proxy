# 🦞 Clacky Bedrock Proxy

让 **Claude Code** 无缝使用 **api.openclacky.com** 的 Claude 模型。

## 为什么需要这个代理？

| | api.openclacky.com | Claude Code 期望 |
|---|---|---|
| API 协议 | **AWS Bedrock Converse** | **Anthropic Messages** |
| 端点 | `POST /model/{id}/converse` | `POST /v1/messages` |
| 认证 | `Authorization: Bearer <key>` | `x-api-key` / `Authorization: Bearer` |
| 模型名 | `abs-claude-sonnet-4-5` | `claude-sonnet-4-5`（且会自动加日期后缀，如 `-20251001`）|
| 消息格式 | `content: [{text: "..."}]` | `content: "..."` |
| 工具格式 | `toolConfig.tools[].toolSpec` | `tools[].input_schema` |

本代理在中间完成**双向协议翻译**，让 Claude Code 以为在跟 Anthropic 官方 API 对话。

## 快速开始

### 1. 安装依赖

推荐用 venv 避免污染系统 Python：

```bash
cd clacky-bedrock-proxy
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. 启动代理

```bash
./venv/bin/python proxy.py
# → 默认监听 http://localhost:8080
```

自定义端口：
```bash
PORT=9090 ./venv/bin/python proxy.py
```

**不再需要** `CLACKY_API_KEY` 环境变量 —— key 通过请求头透传（见下一步）。

### 3. 在 Claude Code 中使用

```bash
export ANTHROPIC_BASE_URL="http://localhost:8080"
export ANTHROPIC_AUTH_TOKEN="你的 api.openclacky.com key"
claude --model claude-haiku-4-5
```

> 💡 **必须用 `ANTHROPIC_AUTH_TOKEN`，不要用 `ANTHROPIC_API_KEY`**。
> 后者会让 Claude Code 走官方鉴权链，直接 401。
> `ANTHROPIC_AUTH_TOKEN` 会作为 `Authorization: Bearer xxx` 透传给代理，代理再传给 api.openclacky.com。

切换模型：在 Claude Code 内部 `/model claude-opus-4-7` 即可。

### 4. 推荐：用配置文件管理 key

为了避免 key 裸露在 shell history 里：

```bash
mkdir -p ~/.config/clacky
cat > ~/.config/clacky/env <<'EOF'
export ANTHROPIC_BASE_URL=http://localhost:8080
export ANTHROPIC_AUTH_TOKEN=你的-clacky-key
EOF
chmod 600 ~/.config/clacky/env

# 用的时候：
source ~/.config/clacky/env && claude
```

### 5. Codex（OpenAI Codex CLI）不走本代理

```bash
# Codex 用 OpenAI 兼容格式，直接连 api.openclacky.com
export OPENAI_API_KEY="你的 clacky key"
export OPENAI_BASE_URL="https://api.openclacky.com/v1"
# 模型名用 dsk-deepseek-v4-pro 或 or-gemini-3-1-pro
```

## 鉴权机制

代理按以下优先级读取 key：

1. `Authorization: Bearer xxx` 请求头（Claude Code 通过 `ANTHROPIC_AUTH_TOKEN` 发送）
2. `x-api-key: xxx` 请求头（Anthropic SDK 默认方式）
3. `CLACKY_API_KEY` 环境变量（兜底，不推荐）

找不到任何 key 时，代理返回 `401 authentication_error`。

## 支持的模型

| Claude Code 中使用的模型 | 实际调用的 api.openclacky.com 模型 |
|---|---|
| `claude-sonnet-4-5` | `abs-claude-sonnet-4-5` |
| `claude-sonnet-4-6` | `abs-claude-sonnet-4-6` |
| `claude-sonnet-4-7` | `abs-claude-sonnet-4-7` |
| `claude-opus-4-7` | `abs-claude-opus-4-7` |
| `claude-opus-4-6` | `abs-claude-opus-4-6` |
| `claude-haiku-4-5` | `abs-claude-haiku-4-5` |
| `claude-sonnet-4` / `claude-opus-4` / `claude-haiku-4` | 对应 `abs-` 前缀版本 |

- 其他未列出的模型名会自动加 `abs-` 前缀
- **日期后缀自动剥离**：Claude Code 会给模型名追加 `-YYYYMMDD`（如 `claude-haiku-4-5-20251001`），代理会自动去掉再匹配映射表

## 架构

```
┌─────────────┐    Anthropic Messages API    ┌───────────────┐    Bedrock Converse API    ┌────────────────────┐
│  Claude Code│ ─────────────────────────────▶│  Clacky Proxy │ ────────────────────────────▶│ api.openclacky.com │
│             │ ◀─────────────────────────────│   :8080       │ ◀────────────────────────────│                    │
└─────────────┘    (协议翻译后)                └───────────────┘    (协议翻译后)              └────────────────────┘
   Bearer token 原样透传  ────────────────────────────────────────────────────────────────▶
```

支持的转换：
- ✅ 纯文本对话
- ✅ 工具调用 (tool use / tool result)
- ✅ 图片输入 (base64)
- ✅ 非流式 + 流式 (SSE) 响应
- ✅ 系统提示词 (system prompt)
- ✅ 模型列表查询 (`/v1/models`)
- ✅ 模型名日期后缀自动剥离
- ⚠️ Thinking (extended thinking) — 取决于后端支持
- ⚠️ Prompt caching — 格式不同，暂未完全转换

## 后台运行

```bash
# 方式 A：nohup
nohup ./venv/bin/python proxy.py > /tmp/clacky-proxy.log 2>&1 &

# 方式 B：tmux
tmux new -s clacky-proxy
./venv/bin/python proxy.py
# Ctrl+B D 分离

# 方式 C：launchd (macOS 开机自启，见下文)
```

### macOS LaunchAgent（开机自启）

创建 `~/Library/LaunchAgents/com.clacky.bedrock-proxy.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.clacky.bedrock-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/zhangrunsheng/clacky_workspace/clacky-bedrock-proxy/venv/bin/python</string>
        <string>/Users/zhangrunsheng/clacky_workspace/clacky-bedrock-proxy/proxy.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PORT</key>
        <string>8080</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/clacky-proxy.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/clacky-proxy.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.clacky.bedrock-proxy.plist
```

> 注意：launchd 方式下代理不持有 key，依然需要客户端通过 `ANTHROPIC_AUTH_TOKEN` 透传。

## 调试

```bash
# 查看日志
tail -f /tmp/clacky-proxy.log

# 查看服务状态 + 可用模型
curl http://localhost:8080/
curl http://localhost:8080/v1/models

# 测试非流式请求（把 KEY 换成真 key）
KEY=clacky-xxxxxxxxxxxxxxxxxxxx
curl -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-haiku-4-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Say hello in Chinese"}]
  }'

# 测试流式请求
curl -N -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-haiku-4-5",
    "max_tokens": 100,
    "stream": true,
    "messages": [{"role": "user", "content": "数 1 到 5"}]
  }'

# 测试日期后缀自动剥离（模拟 Claude Code 行为）
curl -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"abs-claude-haiku-4-5-20251001","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'

# 无 key 应返回 401
curl -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `CLACKY_API_KEY` | ❌ | — | 兜底 key（不推荐，优先用请求头透传） |
| `CLACKY_BACKEND_URL` | ❌ | `https://api.openclacky.com` | 后端地址 |
| `PORT` | ❌ | `8080` | 代理监听端口 |

## 常见问题

**Q：`/model` 切换模型时报 `unknown model abs-claude-xxx-4-5-20251001` 400 错误？**
A：已修复。旧版本没有剥离 Claude Code 自动追加的日期后缀，现在代理会自动处理。拉最新 proxy.py 即可。

**Q：设置了 `ANTHROPIC_API_KEY` 但 Claude Code 报 401？**
A：Claude Code 识别到 `ANTHROPIC_API_KEY` 会走官方鉴权链。**必须改用 `ANTHROPIC_AUTH_TOKEN`**。

**Q：代理启动报 `ModuleNotFoundError: No module named 'fastapi'`？**
A：系统 Python 没装依赖，用 venv 里的 Python：`./venv/bin/python proxy.py`。

**Q：代理启动报 Python 2 语法错误？**
A：macOS 系统 `python` 命令可能指向 Python 2，用 `python3` 或 venv 里的解释器。

## License

MIT — 跟 openclacky 保持一致。
