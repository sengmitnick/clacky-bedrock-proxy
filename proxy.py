#!/usr/bin/env python3
"""
Clacky Bedrock Proxy — 让 Claude Code 无缝使用 api.openclacky.com

Anthropic Messages API ↔ AWS Bedrock Converse API 双向协议翻译

融合 aws-samples/sample-bedrock-api-proxy 的官方转换逻辑。
"""

import base64
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncIterator, Optional, List

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.background import BackgroundTask

# ── Configuration ──────────────────────────────────────────────────────────

API_KEY = os.environ.get("CLACKY_API_KEY", "")  # 兜底用；更推荐走请求头传入
BACKEND_URL = os.environ.get(
    "CLACKY_BACKEND_URL",
    "https://api.openclacky.com",
)
# 模型映射：Claude Code 用的模型名 → api.openclacky.com 的模型名
MODEL_MAP = {
    "claude-sonnet-4-5":  "abs-claude-sonnet-4-5",
    "claude-sonnet-4-6":  "abs-claude-sonnet-4-6",
    "claude-sonnet-4-7":  "abs-claude-sonnet-4-6",
    "claude-opus-4-7":    "abs-claude-opus-4-7",
    "claude-opus-4-6":    "abs-claude-opus-4-6",
    "claude-haiku-4-5":   "abs-claude-haiku-4-5",
    "claude-sonnet-4":    "abs-claude-sonnet-4-5",  # 别名
    "claude-opus-4":      "abs-claude-opus-4-7",     # 别名
    "claude-haiku-4":     "abs-claude-haiku-4-5",    # 别名
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("clacky-proxy")

app = FastAPI(title="Clacky Bedrock Proxy", version="0.1.0")

# ── Model name utilities ──────────────────────────────────────────────────

_DATE_SUFFIX_RE = re.compile(r"-\d{8}$")


def _strip_date_suffix(name: str) -> str:
    """剥掉 Claude Code 默认加的 -YYYYMMDD 日期后缀"""
    return _DATE_SUFFIX_RE.sub("", name)


def to_bedrock_model(anthropic_model: str) -> str:
    """将 Claude Code 的模型名映射到 Bedrock 前缀模型名"""
    # 1. 先剥日期后缀（Claude Code 默认加 -20251001 这类）
    model = _strip_date_suffix(anthropic_model)
    # 2. 命中映射表
    if model in MODEL_MAP:
        return MODEL_MAP[model]
    # 3. 已带 abs- 前缀，原样返回
    if model.startswith("abs-"):
        return model
    # 4. OpenRouter 风格 anthropic/xxx
    if model.startswith("anthropic/"):
        base = model.replace("anthropic/", "")
        return to_bedrock_model(base)
    # 5. 默认加 abs- 前缀
    return f"abs-{model}"


def to_anthropic_model(bedrock_model: str) -> str:
    """去掉 abs- 前缀还原模型名"""
    if bedrock_model.startswith("abs-"):
        return bedrock_model[4:]
    return bedrock_model


# ── HTTP client ───────────────────────────────────────────────────────────

CLIENT_CONFIG = {
    "timeout": httpx.Timeout(300.0, connect=10.0),
    "limits": httpx.Limits(max_keepalive_connections=20),
}


def extract_api_key(request: Request) -> str:
    """
    从请求头提取 api.openclacky.com 的 key，优先级：
      1. Authorization: Bearer xxx  （Claude Code 的 ANTHROPIC_AUTH_TOKEN 走这个）
      2. x-api-key: xxx              （Anthropic SDK 默认走这个）
      3. CLACKY_API_KEY 环境变量     （兜底）
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    xkey = request.headers.get("x-api-key", "").strip()
    if xkey:
        return xkey
    return API_KEY


def bedrock_client(api_key: Optional[str] = None) -> httpx.AsyncClient:
    key = api_key or API_KEY
    return httpx.AsyncClient(
        base_url=BACKEND_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        **CLIENT_CONFIG,
    )


# ── Request conversion: Anthropic → Bedrock ──────────────────────────────

def convert_system_to_bedrock(system) -> list[dict]:
    """Anthropic system → Bedrock system 数组"""
    if system is None:
        return []
    if isinstance(system, str):
        if system.strip():
            return [{"text": system}]
        return []
    if isinstance(system, list):
        texts = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                if t.strip():
                    texts.append(t)
            elif isinstance(block, str):
                if block.strip():
                    texts.append(block)
        if texts:
            return [{"text": "\n\n".join(texts)}]
    return []


def convert_content_block_to_bedrock(block: Any):
    """单个 Anthropic content block → Bedrock block"""
    if isinstance(block, str):
        return {"text": block} if block else None

    if not isinstance(block, dict):
        return {"text": str(block)}

    block_type = block.get("type", "")

    if block_type == "text":
        t = block.get("text", "")
        result = {"text": t} if t else None
        # Prompt Caching: 文本块有 cache_control → 追加 cachePoint
        if result and block.get("cache_control"):
            # 只在有实际内容时添加 cachePoint
            return [result, {"cachePoint": {"type": "default"}}]
        return result

    elif block_type == "tool_use":
        input_data = block.get("input", {})
        if isinstance(input_data, str):
            try:
                input_data = json.loads(input_data)
            except json.JSONDecodeError:
                pass
        return {
            "toolUse": {
                "toolUseId": block.get("id", ""),
                "name": block.get("name", ""),
                "input": input_data,
            }
        }

    elif block_type == "tool_result":
        result_content = block.get("content", "")
        if isinstance(result_content, str):
            result_blocks = [{"text": result_content}]
        elif isinstance(result_content, list):
            result_blocks = [
                {"text": c.get("text", str(c))} if isinstance(c, dict) else {"text": str(c)}
                for c in result_content
            ]
        else:
            result_blocks = [{"text": str(result_content)}]
        return {
            "toolResult": {
                "toolUseId": block.get("tool_use_id", ""),
                "content": result_blocks,
                "status": "error" if block.get("is_error") else "success",
            }
        }

    elif block_type == "image":
        source = block.get("source", {})
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image/png")
            fmt = media_type.split("/")[-1] if "/" in media_type else media_type
            # Bedrock HTTP API 的 bytes 字段接受 base64 字符串，直接传原始 data，无需 decode
            return {
                "image": {
                    "format": fmt,
                    "source": {"bytes": source.get("data", "")},
                }
            }
        return None

    elif block_type == "image_url":
        url = block.get("image_url", {}).get("url", "")
        if url.startswith("data:"):
            match = re.match(r"^data:image/([^;]+);base64,(.*)$", url)
            if match:
                # Bedrock HTTP API 的 bytes 字段接受 base64 字符串，直接传，无需 decode
                return {
                    "image": {
                        "format": match.group(1),
                        "source": {"bytes": match.group(2)},
                    }
                }
        return None

    elif block_type == "thinking":
        # Extended thinking input → reasoningContent
        return {
            "reasoningContent": {
                "reasoningText": {
                    "text": block.get("thinking", ""),
                    **({"signature": block["signature"]} if block.get("signature") else {}),
                }
            }
        }

    elif block_type == "redacted_thinking":
        # Redacted thinking → reasoningContent redacted
        return {
            "reasoningContent": {
                "redactedContent": block.get("data", "")
            }
        }

    # 透传 Bedrock 原生块 (cachePoint, reasoningContent 等)
    if "cachePoint" in block or "reasoningContent" in block:
        return block

    return None


def convert_message_to_bedrock(msg: dict) -> Optional[dict]:
    """Anthropic message → Bedrock message"""
    role = msg.get("role", "user")
    content = msg.get("content", "")

    # 处理 string content
    if isinstance(content, str):
        blocks = [{"text": content}] if content else [{"text": "..."}]
        return {"role": role, "content": blocks}

    # 处理 content array
    if isinstance(content, list):
        blocks = []
        for block in content:
            converted = convert_content_block_to_bedrock(block)
            if converted is None:
                continue
            # 展开列表（如 text + cachePoint）
            if isinstance(converted, list):
                blocks.extend(converted)
            else:
                blocks.append(converted)

        # Bedrock 要求至少一个 content block
        if not blocks:
            blocks = [{"text": "..."}]

        # tool_result 消息在 Bedrock 里必须是 user 角色
        has_tool_result = any(b.get("toolResult") for b in blocks)
        if has_tool_result:
            role = "user"

        return {"role": role, "content": blocks}

    return {"role": role, "content": [{"text": str(content)}]}


def merge_consecutive_tool_results(messages: list[dict]) -> list[dict]:
    """合并连续的工具结果消息（Bedrock 要求严格的 user/assistant 交替）"""
    if not messages:
        return messages

    merged = []
    for msg in messages:
        prev = merged[-1] if merged else None
        if (
            prev
            and prev["role"] == "user"
            and msg["role"] == "user"
            and isinstance(prev.get("content"), list)
            and isinstance(msg.get("content"), list)
            and any(b.get("toolResult") for b in prev["content"])
            and any(b.get("toolResult") for b in msg["content"])
        ):
            merged[-1]["content"].extend(msg["content"])
        else:
            merged.append(msg.copy())
    return merged


def convert_tools_to_bedrock(tools: Optional[List[dict]], tool_choice: Any = None) -> Optional[dict]:
    """Anthropic tools → Bedrock toolConfig"""
    if not tools:
        return None

    bedrock_tools = []
    for tool in tools:
        bedrock_tools.append({
            "toolSpec": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "inputSchema": {"json": tool.get("input_schema", {})},
            }
        })

    config = {"tools": bedrock_tools}

    # Tool Choice 支持（来自 aws-samples 官方转换器）
    if tool_choice:
        if isinstance(tool_choice, str):
            if tool_choice == "auto":
                config["toolChoice"] = {"auto": {}}
            elif tool_choice == "any":
                config["toolChoice"] = {"any": {}}
        elif isinstance(tool_choice, dict):
            tc_type = tool_choice.get("type", "")
            if tc_type == "tool":
                config["toolChoice"] = {"tool": {"name": tool_choice.get("name", "")}}
            elif tc_type == "auto":
                config["toolChoice"] = {"auto": {}}
            elif tc_type == "any":
                config["toolChoice"] = {"any": {}}

    return config


def _has_tool_blocks(messages: list[dict]) -> bool:
    """检查消息列表中是否包含 toolUse 或 toolResult 块"""
    for msg in messages:
        for block in msg.get("content", []):
            if isinstance(block, dict) and ("toolUse" in block or "toolResult" in block):
                return True
    return False


def build_bedrock_request(
    messages: list[dict],
    model: str,
    system,
    tools: Optional[List[dict]],
    max_tokens: int,
    stop_sequences: Optional[List[str]] = None,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    top_k: Optional[int] = None,
    thinking: Optional[dict] = None,
    tool_choice: Any = None,
) -> dict:
    """构建完整的 Bedrock Converse API 请求体"""
    bedrock_model = to_bedrock_model(model)

    # System
    system_blocks = convert_system_to_bedrock(system)

    # Regular messages (排除 system 角色)
    regular = [m for m in messages if m.get("role") != "system"]
    bedrock_messages = []
    for msg in regular:
        converted = convert_message_to_bedrock(msg)
        if converted:
            bedrock_messages.append(converted)

    # 合并连续 tool_result 消息
    bedrock_messages = merge_consecutive_tool_results(bedrock_messages)

    body = {"messages": bedrock_messages}

    if system_blocks:
        body["system"] = system_blocks

    # Inference config
    inference = {"maxTokens": max_tokens}
    if stop_sequences:
        inference["stopSequences"] = stop_sequences
    if temperature is not None:
        inference["temperature"] = temperature
    if top_p is not None:
        inference["topP"] = top_p
    if top_k is not None:
        inference["topK"] = top_k
    body["inferenceConfig"] = inference

    # Tool config — Bedrock 要求有 toolUse/toolResult 时必须提供 toolConfig
    tool_config = convert_tools_to_bedrock(tools, tool_choice)
    if tool_config:
        body["toolConfig"] = tool_config
    elif _has_tool_blocks(bedrock_messages):
        # 消息中包含 toolUse/toolResult 但没有 tools → 补充空的 toolConfig
        body["toolConfig"] = {"tools": []}

    return bedrock_model, body


# ── Response conversion: Bedrock → Anthropic ─────────────────────────────

def convert_bedrock_block_to_anthropic(block: dict) -> Optional[dict]:
    """Bedrock content block → Anthropic block"""
    if "text" in block:
        return {"type": "text", "text": block["text"]}

    if "toolUse" in block:
        tu = block["toolUse"]
        input_val = tu.get("input", {})
        return {
            "type": "tool_use",
            "id": tu.get("toolUseId", ""),
            "name": tu.get("name", ""),
            "input": input_val,
        }

    if "reasoningContent" in block:
        rc = block["reasoningContent"]
        if "reasoningText" in rc:
            rt = rc["reasoningText"]
            return {
                "type": "thinking",
                "thinking": rt.get("text", ""),
                **({"signature": rt["signature"]} if rt.get("signature") else {}),
            }
        elif "redactedContent" in rc:
            return {
                "type": "redacted_thinking",
                "data": rc["redactedContent"],
            }

    if "image" in block:
        img = block["image"]
        fmt = img.get("format", "png")
        raw_bytes = img.get("source", {}).get("bytes", b"")
        if isinstance(raw_bytes, str):
            raw_bytes = raw_bytes.encode()
        data_base64 = base64.b64encode(raw_bytes).decode()
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": f"image/{fmt}",
                "data": data_base64,
            },
        }

    return None


def convert_bedrock_response(
    bedrock_data: dict, bedrock_model: str
) -> dict:
    """Bedrock Converse 响应 → Anthropic Messages 响应"""
    output = bedrock_data.get("output", {})
    msg = output.get("message", {})
    blocks = msg.get("content", [])
    usage = bedrock_data.get("usage", {})

    # 转换 content blocks
    anthropic_content = []
    for block in blocks:
        converted = convert_bedrock_block_to_anthropic(block)
        if converted:
            anthropic_content.append(converted)

    # stopReason 映射
    stop_reason_map = {
        "end_turn": "end_turn",
        "tool_use": "tool_use",
        "max_tokens": "max_tokens",
        "stop_sequence": "stop_sequence",
        "guardrail_intervened": "end_turn",
        "content_filtered": "end_turn",
    }
    stop_reason = stop_reason_map.get(
        bedrock_data.get("stopReason", "end_turn"), "end_turn"
    )

    # Usage 映射
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)
    cache_read = usage.get("cacheReadInputTokens", 0)
    cache_write = usage.get("cacheWriteInputTokens", 0)

    anthropic_usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if cache_read > 0:
        anthropic_usage["cache_read_input_tokens"] = cache_read
    if cache_write > 0:
        anthropic_usage["cache_creation_input_tokens"] = cache_write

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": to_anthropic_model(bedrock_model),
        "content": anthropic_content,
        "stop_reason": stop_reason,
        "usage": anthropic_usage,
    }


def convert_bedrock_error(status_code: int, error_body: str) -> dict:
    """Bedrock 错误 → Anthropic 格式错误"""
    try:
        data = json.loads(error_body) if error_body else {}
        msg = data.get("message", error_body)
    except json.JSONDecodeError:
        msg = error_body

    return {
        "type": "error",
        "error": {
            "type": "api_error",
            "message": str(msg),
        },
    }


# ── Streaming conversion ─────────────────────────────────────────────────

async def convert_stream(
    bedrock_model: str, response: httpx.Response
) -> AsyncIterator[str]:
    """将 Bedrock 的 SSE 流转换为 Anthropic 格式的 SSE 流"""
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    anthropic_model = to_anthropic_model(bedrock_model)
    content_blocks: list[dict] = []
    current_block_index = -1
    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"

    try:
        async for raw_line in response.aiter_lines():
            if not raw_line:
                continue

            # 处理 SSE 格式 (可能是 "data: {...}" 或直接 JSON)
            line = raw_line.strip()
            data_str = line
            if line.startswith("data:"):
                data_str = line[5:].strip()

            if not data_str:
                continue

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                # 尝试提取行内 JSON
                match = re.search(r'\{.*\}', data_str)
                if match:
                    try:
                        event = json.loads(match.group())
                    except json.JSONDecodeError:
                        continue
                else:
                    continue

            # 处理各种 Bedrock 流事件
            if "messageStart" in event:
                # messageStart: { role: "assistant" }
                yield f"data: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': anthropic_model, 'content': [], 'usage': {'input_tokens': input_tokens}}})}\n\n"

            elif "contentBlockStart" in event:
                cbs = event["contentBlockStart"]
                current_block_index = cbs.get("contentBlockIndex", 0)
                start = cbs.get("start", {})

                if "toolUse" in start:
                    tu = start["toolUse"]
                    block = {
                        "type": "tool_use",
                        "id": tu.get("toolUseId", ""),
                        "name": tu.get("name", ""),
                        "input": {},
                    }
                elif "reasoningContent" in start:
                    rc = start["reasoningContent"]
                    if "redactedContent" in rc:
                        block = {"type": "redacted_thinking", "data": ""}
                    else:
                        block = {"type": "thinking", "thinking": ""}
                else:
                    block = {"type": "text", "text": ""}

                while len(content_blocks) <= current_block_index:
                    content_blocks.append(None)
                content_blocks[current_block_index] = block

                yield f"data: {json.dumps({'type': 'content_block_start', 'index': current_block_index, 'content_block': block})}\n\n"

            elif "contentBlockDelta" in event:
                delta = event["contentBlockDelta"]
                idx = delta.get("contentBlockIndex", current_block_index)
                d = delta.get("delta", {})

                if "reasoningContent" in d:
                    reasoning_text = d["reasoningContent"]
                    if isinstance(reasoning_text, dict):
                        reasoning_text = reasoning_text.get("text", "")
                    yield f"data: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'thinking_delta', 'thinking': reasoning_text}})}\n\n"

                elif "text" in d:
                    yield f"data: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'text_delta', 'text': d['text']}})}\n\n"

                elif "toolUse" in d:
                    tool_delta = d["toolUse"]
                    input_chunk = tool_delta.get("input", "")
                    if isinstance(input_chunk, str):
                        yield f"data: {json.dumps({'type': 'content_block_delta', 'index': idx, 'delta': {'type': 'input_json_delta', 'partial_json': input_chunk}})}\n\n"

            elif "contentBlockStop" in event:
                idx = event.get("contentBlockStop", {}).get("contentBlockIndex", current_block_index)
                yield f"data: {json.dumps({'type': 'content_block_stop', 'index': idx})}\n\n"

            elif "messageStop" in event:
                ms = event["messageStop"]
                stop_reason_map = {
                    "end_turn": "end_turn",
                    "tool_use": "tool_use",
                    "max_tokens": "max_tokens",
                }
                stop_reason = stop_reason_map.get(
                    ms.get("stopReason", "end_turn"), "end_turn"
                )

                yield f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}})}\n\n"
                yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"

            elif "metadata" in event:
                meta = event["metadata"]
                usage = meta.get("usage", {})
                input_tokens = usage.get("inputTokens", 0)
                output_tokens = usage.get("outputTokens", 0)

            elif "exception" in event or "error" in event:
                error_msg = event.get("exception", event.get("error", "Unknown error"))
                error_code = event.get("exception", "internal_error")
                # 映射 AWS 错误码到 Anthropic 格式
                error_type_map = {
                    "ThrottlingException": "rate_limit_error",
                    "TooManyRequestsException": "rate_limit_error",
                    "ServiceUnavailableException": "api_error",
                    "ValidationException": "invalid_request_error",
                    "AccessDeniedException": "permission_error",
                }
                error_type = error_type_map.get(str(error_code), "api_error")
                yield f"data: {json.dumps({'type': 'error', 'error': {'type': error_type, 'message': str(error_msg)}})}\n\n"
                return

    except Exception as e:
        logger.error(f"Stream conversion error: {e}")
        yield f"data: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {
        "service": "Clacky Bedrock Proxy",
        "version": "0.1.0",
        "backend": BACKEND_URL,
        "available_models": list(MODEL_MAP.keys()),
    }


@app.get("/v1/models")
async def list_models():
    """返回可用模型列表（Anthropic 格式）"""
    models_data = []
    for anthropic_name, bedrock_name in MODEL_MAP.items():
        # 只列出主模型，不列别名
        if anthropic_name in (
            "claude-sonnet-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-haiku-4-5",
        ):
            models_data.append({
                "id": anthropic_name,
                "type": "model",
                "display_name": anthropic_name.replace("-", " ").title(),
                "created_at": "2024-01-01T00:00:00Z",
            })

    return {"data": models_data, "has_more": False, "first_id": models_data[0]["id"] if models_data else None}


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    """单个模型信息"""
    if model_id in MODEL_MAP:
        bedrock_name = MODEL_MAP[model_id]
        return {
            "id": model_id,
            "type": "model",
            "display_name": model_id.replace("-", " ").title(),
            "created_at": "2024-01-01T00:00:00Z",
        }
    # 尝试映射
    bedrock = to_bedrock_model(model_id)
    return {
        "id": model_id,
        "type": "model",
        "display_name": model_id.replace("-", " ").title(),
        "created_at": "2024-01-01T00:00:00Z",
    }


@app.post("/v1/messages")
async def messages(request: Request):
    """核心端点：Anthropic Messages API → Bedrock Converse API"""
    # 从请求头提取 api.openclacky.com 的 key
    api_key = extract_api_key(request)
    if not api_key:
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": "Missing API key. Set ANTHROPIC_AUTH_TOKEN (Claude Code) / x-api-key header, or CLACKY_API_KEY env var.",
                },
            },
        )

    body = await request.json()

    model = body.get("model", "claude-sonnet-4-5")
    messages_data = body.get("messages", [])
    system = body.get("system")
    tools = body.get("tools")
    max_tokens = body.get("max_tokens", 4096)
    stream = body.get("stream", False)
    stop_sequences = body.get("stop_sequences")
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    top_k = body.get("top_k")
    thinking = body.get("thinking")
    tool_choice = body.get("tool_choice")

    # 构建 Bedrock 请求
    bedrock_model, bedrock_body = build_bedrock_request(
        messages=messages_data,
        model=model,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
        stop_sequences=stop_sequences,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        thinking=thinking,
        tool_choice=tool_choice,
    )

    logger.info(
        f"→ Bedrock: {bedrock_model} | "
        f"msgs={len(bedrock_body.get('messages', []))} | "
        f"tools={len(tools or [])} | "
        f"stream={stream}"
    )

    endpoint = f"/model/{bedrock_model}/converse"

    async with bedrock_client(api_key) as client:
        try:
            if stream:
                # api.openclacky.com 目前只支持非流式 Bedrock Converse API。
                # Claude Code 强制要求 SSE 流式响应，所以我们先做非流式请求，
                # 然后模拟 Anthropic SSE 格式输出给客户端。
                bedrock_resp = await client.post(endpoint, json=bedrock_body)

                if bedrock_resp.status_code != 200:
                    error_body = convert_bedrock_error(
                        bedrock_resp.status_code,
                        bedrock_resp.text,
                    )
                    return JSONResponse(
                        status_code=bedrock_resp.status_code,
                        content=error_body,
                    )

                bedrock_data = bedrock_resp.json()
                anthropic_resp = convert_bedrock_response(
                    bedrock_data, bedrock_model
                )

                async def simulate_stream() -> AsyncIterator[str]:
                    """将非流式响应模拟为 Anthropic SSE 流"""
                    msg = anthropic_resp
                    msg_id = msg["id"]
                    model_name = msg["model"]
                    content = msg["content"]
                    stop_reason = msg["stop_reason"]
                    usage = msg.get("usage", {})

                    # message_start
                    yield f"data: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'model': model_name, 'content': [], 'usage': {'input_tokens': usage.get('input_tokens', 0)}}})}\n\n"

                    for i, block in enumerate(content):
                        # content_block_start
                        if block["type"] == "tool_use":
                            cb = {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "input": {}}
                        elif block["type"] in ("thinking", "redacted_thinking"):
                            cb = {"type": block["type"], "thinking": "" if block["type"] == "thinking" else None, "data": "" if block["type"] == "redacted_thinking" else None}
                        else:
                            cb = {"type": "text", "text": ""}
                        yield f"data: {json.dumps({'type': 'content_block_start', 'index': i, 'content_block': cb})}\n\n"

                        # content_block_delta
                        if block["type"] == "tool_use":
                            input_json = json.dumps(block.get("input", {}), ensure_ascii=False)
                            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': input_json}})}\n\n"
                        elif block["type"] == "thinking":
                            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'thinking_delta', 'thinking': block.get('thinking', '')}})}\n\n"
                        elif block["type"] == "redacted_thinking":
                            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'redacted_thinking', 'data': block.get('data', '')}})}\n\n"
                        else:
                            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': i, 'delta': {'type': 'text_delta', 'text': block.get('text', '')}})}\n\n"

                        # content_block_stop
                        yield f"data: {json.dumps({'type': 'content_block_stop', 'index': i})}\n\n"

                    # message_delta + message_stop
                    output_tokens = usage.get("output_tokens", 0)
                    yield f"data: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': {'output_tokens': output_tokens}})}\n\n"
                    yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"

                return StreamingResponse(
                    simulate_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            else:
                # 非流式请求
                bedrock_resp = await client.post(endpoint, json=bedrock_body)

                if bedrock_resp.status_code != 200:
                    error_body = convert_bedrock_error(
                        bedrock_resp.status_code,
                        bedrock_resp.text,
                    )
                    return JSONResponse(
                        status_code=bedrock_resp.status_code,
                        content=error_body,
                    )

                bedrock_data = bedrock_resp.json()
                anthropic_resp = convert_bedrock_response(
                    bedrock_data, bedrock_model
                )
                return JSONResponse(content=anthropic_resp)

        except httpx.TimeoutException:
            return JSONResponse(
                status_code=504,
                content={
                    "type": "error",
                    "error": {
                        "type": "timeout",
                        "message": "Backend request timed out",
                    },
                },
            )
        except Exception as e:
            logger.error(f"Proxy error: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={
                    "type": "error",
                    "error": {
                        "type": "proxy_error",
                        "message": str(e),
                    },
                },
            )


# ── Health check ──────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "backend": BACKEND_URL}


# ── Run ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7878"))
    print(f"🦞 Clacky Bedrock Proxy starting on http://localhost:{port}")
    print(f"   Backend: {BACKEND_URL}")
    print(f"   Models:  {list(MODEL_MAP.keys())}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
