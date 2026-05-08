# 🦞 Clacky Bedrock Proxy

让 **Claude Code** 无缝使用 **api.openclacky.com** 的 Claude 模型。

## 为什么需要这个代理？

| | api.openclacky.com | Claude Code 期望 |
|---|---|---|
| API 协议 | **AWS Bedrock Converse** | **Anthropic Messages** |
| 端点 | `POST /model/{id}/converse` | `POST /v1/messages` |
| 认证 | `Authorization: Bearer <key>` | `x-api-key: <key>` |
| 模型名 | `abs-claude-sonnet-4-5` | `claude-sonnet-4-5` |
| 消息格式 | `content: [{text: "..."}]` | `content: "..."` |
| 工具格式 | `toolConfig.tools[].toolSpec` | `tools[].input_schema` |

本代理在中间完成**双向协议翻译**，让 Claude Code 以为在跟 Anthropic 官方 API 对话。

## 快速开始

### 1. 安装依赖

```bash
cd clacky-bedrock-proxy
pip install -r requirements.txt
```

### 2. 配置 API Key

```bash
export CLACKY_API_KEY="你的 api.openclacky.com key"
```

### 3. 启动代理

```bash
python proxy.py
# → 默认监听 http://localhost:8080
```

自定义端口：
```bash
PORT=9090 python proxy.py
```

### 4. 在 Claude Code 中使用

在 Claude Code 中配置：

```bash
# 方法 A：环境变量
export ANTHROPIC_BASE_URL="http://localhost:8080"
export ANTHROPIC_API_KEY="any-value"  # 代理会忽略这个，用 CLACKY_API_KEY

# 方法 B：Claude Code 配置文件
# ~/.claude/settings.json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8080",
    "ANTHROPIC_API_KEY": "proxy"
  }
}
```

启动 Claude Code：
```bash
claude
```

### 5. 在 Codex（OpenAI Codex CLI）中使用

```bash
# Codex 直接用 OpenAI 兼容格式，不需要此代理
# 直接配置：
export OPENAI_API_KEY="你的 clacky key"
export OPENAI_BASE_URL="https://api.openclacky.com/v1"
# 模型名用 dsk-deepseek-v4-pro 或 or-gemini-3-1-pro
```

## 支持的模型

| Claude Code 中使用的模型 | 实际调用的 api.openclacky.com 模型 |
|---|---|
| `claude-sonnet-4-5` | `abs-claude-sonnet-4-5` |
| `claude-sonnet-4-6` | `abs-claude-sonnet-4-6` |
| `claude-opus-4-7` | `abs-claude-opus-4-7` |
| `claude-opus-4-6` | `abs-claude-opus-4-6` |
| `claude-haiku-4-5` | `abs-claude-haiku-4-5` |

其他未列出的模型名会自动加 `abs-` 前缀。

## 架构

```
┌─────────────┐     Anthropic Messages API     ┌───────────────┐     Bedrock Converse API     ┌────────────────────┐
│  Claude Code │ ──────────────────────────────→│  Clacky Proxy │ ───────────────────────────→│ api.openclacky.com │
│             │ ←──────────────────────────────│   :8080       │ ←───────────────────────────│                    │
└─────────────┘     (协议翻译后)                └───────────────┘     (协议翻译后)              └────────────────────┘
```

支持的转换：
- ✅ 纯文本对话
- ✅ 工具调用 (tool use / tool result)
- ✅ 图片输入 (base64)
- ✅ 非流式 + 流式 (SSE) 响应
- ✅ 系统提示词 (system prompt)
- ✅ 模型列表查询 (`/v1/models`)
- ⚠️ Thinking (extended thinking) — 取决于后端支持
- ⚠️ Prompt caching — 格式不同，暂未完全转换

## 后台运行

```bash
# 使用 nohup
nohup python proxy.py > proxy.log 2>&1 &

# 或使用 tmux
tmux new -s clacky-proxy
python proxy.py
# Ctrl+B D 分离

# 或使用 launchd (macOS)
# 见下文
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
        <string>/usr/bin/python3</string>
        <string>/Users/zhangrunsheng/clacky_workspace/clacky-bedrock-proxy/proxy.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CLACKY_API_KEY</key>
        <string>你的key</string>
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

## 调试

```bash
# 查看日志
tail -f /tmp/clacky-proxy.log

# 手动测试
curl http://localhost:8080/health
curl http://localhost:8080/v1/models

# 测试非流式请求
curl -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: any" \
  -d '{
    "model": "claude-haiku-4-5",
    "max_tokens": 100,
    "messages": [{"role": "user", "content": "Say hello in Chinese"}]
  }'

# 测试流式请求
curl -X POST http://localhost:8080/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: any" \
  -d '{
    "model": "claude-haiku-4-5",
    "max_tokens": 100,
    "stream": true,
    "messages": [{"role": "user", "content": "Say hello in Chinese"}]
  }'
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `CLACKY_API_KEY` | ✅ | — | api.openclacky.com 的 API key |
| `CLACKY_BACKEND_URL` | ❌ | `https://api.openclacky.com` | 后端地址 |
| `PORT` | ❌ | `8080` | 代理监听端口 |

## License

MIT — 跟 openclacky 保持一致。
