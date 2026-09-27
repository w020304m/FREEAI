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


def openai_reasoning_chunk(request_id: str, model: str, delta: str) -> Dict[str, Any]:
    """思考增量(DeepSeek 社区约定:delta.reasoning_content),主流客户端(Cherry Studio 等)可渲染。"""
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": now_ts(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"reasoning_content": delta} if delta else {},
                "finish_reason": None,
                "logprobs": None,
            }
        ],
    }


def openai_full(request_id: str, model: str, content: str, reasoning: Optional[str] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": now_ts(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
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


# ---- 多块(index 跟踪)与 thinking 块:provider 思考流透传用 ----

def anthropic_text_delta_at(index: int, text: str) -> str:
    return sse(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def anthropic_block_start_at(index: int) -> str:
    return sse(
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
        }
    )


def anthropic_block_stop_at(index: int) -> str:
    return sse({"type": "content_block_stop", "index": index})


def anthropic_thinking_block_start(index: int) -> str:
    return sse(
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": ""},
        }
    )


def anthropic_thinking_delta(index: int, text: str) -> str:
    return sse(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        }
    )


def anthropic_stop() -> str:
    return sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}}) + sse(
        {"type": "message_stop"}
    )


def anthropic_full(model: str, content: str, request_id: str, reasoning: Optional[str] = None) -> Dict[str, Any]:
    blocks: List[Dict[str, Any]] = []
    if reasoning:
        blocks.append({"type": "thinking", "thinking": reasoning})
    blocks.append({"type": "text", "text": content})
    return {
        "id": request_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# ---------------------------------------------------------------------------
# 原生 tools 协议适配（上游无 tools 字段 → 注入 prompt 协议 → 解析输出还原成原生格式）
# 使 OpenAI SDK tools 参数 / Anthropic SDK tools 参数 / Claude Code 等客户端可直接用工具调用。
# ---------------------------------------------------------------------------

TOOL_MARKER = "<<TOOL_CALL>>"  # 旧标记,解析仍兼容
# 解析兼容的标记族:开源模型(Qwen/GLM/Hermes)训练时见过 <tool_call> 格式,依从率更高;
# [TOOL_CALLS] 为 Mistral 系格式;<<TOOL_CALL>> 为本网关旧协议
TOOL_MARKERS = ("<<TOOL_CALL>>", "<tool_call>", "[TOOL_CALLS]")


def contains_tool_marker(text: str) -> bool:
    """输出里是否残留任何协议标记(用于未命中工具时的清理判定)。"""
    if not text:
        return False
    return any(m in text for m in TOOL_MARKERS) or '"tool_call"' in text


def _minify_schema(params: Any) -> Any:
    """压缩 JSON Schema:去 title/examples/$schema 等对模型决策无用的重字段,省 token。"""
    if isinstance(params, dict):
        return {k: _minify_schema(v) for k, v in params.items()
                if k not in ("title", "examples", "$schema")}
    if isinstance(params, list):
        return [_minify_schema(v) for v in params]
    return params


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
            "parameters": _minify_schema(fn.get("parameters") or fn.get("input_schema") or {}),
        })
    return out


def tools_prompt(tools: Any, tool_choice: Any = None) -> str:
    """把工具清单渲染成紧凑协议段(上游无原生 tools,只能 prompt 模拟)。

    设计要点:
    - Schema 最小化+紧凑序列化(省 token,无缩进无空格);
    - 输出标记用 <tool_call>:Qwen/GLM/Hermes 系开源模型训练时见过,依从率更高;
    - 必须覆盖 agent 循环:调用 → [tool_result] 回传 → 续跑/收尾;
    - tool_choice=required 时追加强制调用规则。"""
    norm = normalize_tools(tools)
    if not norm:
        return ""
    required = isinstance(tool_choice, dict) and tool_choice.get("type") == "function" \
        or tool_choice == "required"
    lines = [
        "",
        "# 工具调用协议",
        "可用工具:",
        json.dumps(norm, ensure_ascii=False, separators=(",", ":")),
        "",
        "规则:",
        '1. 需要调用工具时,独占一行输出: <tool_call>{"name":"工具名","arguments":{...}}</tool_call>',
        "2. 每次回复最多一个工具调用;arguments 须符合该工具 parameters。",
        "3. 工具结果以 [tool_result] 开头出现在后续用户消息中;收到后未完成则继续调用,已完成则直接给出最终回答。",
        "4. 无需工具时正常回答;不要输出 <tool_call>,不要解释本协议。",
    ]
    if required:
        lines.append("5. 本轮必须调用一个工具,不得直接给最终答案。")
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


def _name_args(obj: Any) -> tuple:
    """从 JSON 对象提取 (name, arguments),兼容多种常见形态:
    {name,arguments} / {name,args} / {name,parameters} / {tool_call:{...}} / {tool:{...}}"""
    if not isinstance(obj, dict):
        return None, None
    core = obj.get("tool_call") or obj.get("tool")
    core = core if isinstance(core, dict) else obj
    name = core.get("name") or core.get("tool_name") or ""
    args = core.get("arguments")
    if args is None:
        args = core.get("args") if core.get("args") is not None else core.get("parameters")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:  # noqa: BLE001
            args = {"_raw": args}
    if not name:
        return None, None
    return name, args if isinstance(args, dict) else {}


def parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """从模型完整输出中解析工具调用,返回 {"name", "arguments"} 或 None。

    兼容形态(按优先级):
    1. 标记行: <tool_call>{...}</tool_call> / <<TOOL_CALL>>{...} / [TOOL_CALLS]{...}
    2. {"tool_call":{"name","arguments"}} 纯 JSON 行
    3. 任意含 name + arguments/args/parameters 的 JSON 对象(取最后一个)
    """
    if not text:
        return None
    # 1) 标记优先:取标记后第一个平衡 JSON
    for marker in TOOL_MARKERS:
        idx = text.find(marker)
        if idx == -1:
            continue
        obj = _extract_json_object(text[idx + len(marker):])
        if obj:
            name, args = _name_args(obj)
            if name:
                return {"name": name, "arguments": args}
    # 2) 无标记:扫描全文,取最后一个形似工具调用的 JSON
    dec = json.JSONDecoder()
    last = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            tc = obj.get("tool_call") or obj.get("tool")
            core = tc if isinstance(tc, dict) else obj
            if core.get("name") and any(k in core for k in ("arguments", "args", "parameters")):
                last = obj
        i = text.find("{", i + end)
    if last is not None:
        name, args = _name_args(last)
        if name:
            return {"name": name, "arguments": args}
    return None


def strip_tool_marker(text: str) -> str:
    """移除输出里的工具协议行,留下自然语言部分(兼容标记族)。"""
    kept = [ln for ln in text.splitlines()
            if not any(m in ln for m in TOOL_MARKERS) and '"tool_call"' not in ln]
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
