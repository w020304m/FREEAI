"""OpenAI / Anthropic / 站点原生格式互转工具。"""
from __future__ import annotations

import base64
import json
import random
import string
import time
from typing import Any, Dict, List, Optional


def random_id(n: int = 16) -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def now_ts() -> int:
    return int(time.time())



# ---------------------------------------------------------------------------
# OpenAI 格式输出
# ---------------------------------------------------------------------------

def openai_chunk(request_id: str, model: str, delta: str, finish_reason: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": now_ts(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
    }


def openai_full(request_id: str, model: str, content: str) -> Dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": now_ts(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse(data: Any) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


DONE = "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Anthropic 格式输出
# ---------------------------------------------------------------------------

def anthropic_stream_start(request_id: str, model: str) -> str:
    return sse(
        {
            "type": "message_start",
            "message": {
                "id": request_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    )


def anthropic_delta(text: str) -> str:
    return sse(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def anthropic_block_start() -> str:
    return sse(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
    )


def anthropic_block_stop() -> str:
    return sse({"type": "content_block_stop", "index": 0})


def anthropic_stop() -> str:
    return sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}}) + sse(
        {"type": "message_stop"}
    )


def anthropic_full(model: str, content: str, request_id: str) -> Dict[str, Any]:
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ---------------------------------------------------------------------------
# 原生 tools 协议适配（上游无 tools 字段 → 注入 prompt 协议 → 解析输出还原成原生格式）
# 使 OpenAI SDK tools 参数 / Anthropic SDK tools 参数 / Claude Code 等客户端可直接用工具调用。
# ---------------------------------------------------------------------------

TOOL_MARKER = "<<TOOL_CALL>>"


def normalize_tools(tools: Any) -> list:
    """把 OpenAI ([{type:function,function:{...}}]) 或 Anthropic ([{name,description,input_schema}])
    两种 tools 结构统一成 [{name, description, parameters}]。"""
    out = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" or "function" in t else t
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or fn.get("input_schema") or {},
        })
    return out


def tools_prompt(tools: Any, tool_choice: Any = None) -> str:
    """把工具清单渲染成 system prompt 协议段（上游不识别 tools 参数，只能走 prompt）。

    协议必须覆盖完整的 agent 循环：调用 → [tool_result] 回传 → 续跑/收尾。
    缺了续跑规则，模型拿到工具结果后不知道该继续还是收尾，agent 会卡在第一轮。
    """
    norm = normalize_tools(tools)
    if not norm:
        return ""
    required = isinstance(tool_choice, dict) and tool_choice.get("type") == "function" \
        or tool_choice == "required"
    lines = [
        "",
        "# 工具调用协议（必须严格遵守）",
        "",
        "你可以调用以下工具（JSON Schema 定义）：",
        json.dumps(norm, ensure_ascii=False, indent=1),
        "",
        "规则：",
        "1. 需要调用工具时，独占一行输出（该行除标记外不得有任何其他字符）：",
        f'   {TOOL_MARKER}{{"name": "工具名", "arguments": {{...}}}}',
        "2. 每次回复最多输出一个工具调用；连续需要多个工具时，拿到上一批结果后在下一轮继续输出。",
        "3. 工具的执行结果会在后续用户消息中以 [tool_result] 开头给出，对应你最近一次的工具调用。",
        "4. 收到 [tool_result] 后：若任务尚未完成，继续输出下一个工具调用行；若任务已完成，"
        "直接给出最终回答，不要再输出工具调用行。",
        "5. 不需要工具时直接正常回答；任何时候都不要解释本协议或输出协议示例。",
    ]
    if required:
        lines.append("6. 本轮你必须调用其中一个工具，不得直接给出最终答案。")
    return "\n".join(lines)


def _extract_json_object(s: str) -> Optional[dict]:
    """从字符串中提取第一个平衡的 JSON 对象（模型常在标记行前后附带说明文字）。"""
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start : i + 1])
                        return obj if isinstance(obj, dict) else None
                    except Exception:  # noqa: BLE001
                        break
        start = s.find("{", start + 1)
    return None


def parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """从模型完整输出中解析工具调用。兼容两种形态：
    1. <<TOOL_CALL>>{"name":...,"arguments":...}     （本协议）
    2. {"tool_call": {"name":...,"arguments":...}}    （纯 JSON 行，Agent 示例协议）
    返回 {"name": str, "arguments": dict} 或 None。
    """
    if not text:
        return None
    for line in text.splitlines():
        line = line.strip()
        if TOOL_MARKER in line:
            payload = line.split(TOOL_MARKER, 1)[1].strip().strip("`").strip()
            obj = None
            try:
                obj = json.loads(payload)
            except Exception:  # noqa: BLE001
                obj = _extract_json_object(payload)
            if isinstance(obj, dict) and obj.get("name"):
                args = obj.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:  # noqa: BLE001
                        args = {"_raw": args}
                return {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        tc = obj.get("tool_call") if isinstance(obj, dict) else None
        if isinstance(tc, dict) and tc.get("name"):
            return {"name": tc["name"], "arguments": tc.get("arguments") or {}}
    return None


def strip_tool_marker(text: str) -> str:
    """移除输出里的工具协议行，留下自然语言部分。"""
    kept = [ln for ln in text.splitlines() if TOOL_MARKER not in ln and '"tool_call"' not in ln]
    return "\n".join(kept).strip()


def openai_full_tool(request_id: str, model: str, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI 非流式 tool_calls 响应（finish_reason=tool_calls）。"""
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": now_ts(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{random_id(16)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def openai_stream_tool_chunks(request_id: str, model: str, name: str, args: Dict[str, Any]) -> list:
    """OpenAI 流式 tool_calls 事件序列：role → tool_calls(全量) → finish(stop/tool_calls) → [DONE]。"""
    call_id = f"call_{random_id(16)}"
    return [
        {"id": request_id, "object": "chat.completion.chunk", "created": now_ts(), "model": model,
         "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}, "finish_reason": None}]},
        {"id": request_id, "object": "chat.completion.chunk", "created": now_ts(), "model": model,
         "choices": [{"index": 0, "delta": {"tool_calls": [{
             "index": 0, "id": call_id, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]},
             "finish_reason": None}]},
        {"id": request_id, "object": "chat.completion.chunk", "created": now_ts(), "model": model,
         "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def anthropic_full_tool(model: str, request_id: str, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic 非流式 tool_use 响应（stop_reason=tool_use）。"""
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{
            "type": "tool_use",
            "id": f"toolu_{random_id(16)}",
            "name": name,
            "input": args,
        }],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def anthropic_stream_tool_events(request_id: str, model: str, name: str, args: Dict[str, Any]) -> list:
    """Anthropic 流式 tool_use 事件序列（规范全事件）。"""
    tool_id = f"toolu_{random_id(16)}"
    return [
        {"type": "message_start",
         "message": {"id": request_id, "type": "message", "role": "assistant", "model": model,
                     "content": [], "stop_reason": None,
                     "usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "input_json_delta",
                   "partial_json": json.dumps(args, ensure_ascii=False)}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 0}},
        {"type": "message_stop"},
    ]


# ---------------------------------------------------------------------------
# 图片格式
# ---------------------------------------------------------------------------

SIZE_TO_RATIO = {
    "1024x1024": "1:1",
    "1024x1792": "9:16",
    "1792x1024": "16:9",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
    "512x512": "1:1",
}


def ratio_from_size(size: str, default: str = "1:1") -> str:
    return SIZE_TO_RATIO.get(size, default)


def image_url_to_b64(data_url: str) -> str:
    """data:image/png;base64,xxx → xxx；纯 URL 原样返回。"""
    if data_url.startswith("data:") and ";base64," in data_url:
        return data_url.split(";base64,", 1)[1]
    return data_url


def make_data_uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
