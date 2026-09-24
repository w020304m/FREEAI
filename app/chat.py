"""聊天补全：OpenAI /v1/chat/completions 与 Anthropic /v1/messages 双协议实现。

上游（逆向确认）：
- GET  /api/chat-nonce            → {nonce}
- POST /api/generate-ai-answer-*（各模型独立端点）
    body: {model, question, tone, format, file, conversationHistory:[{role,content}],
           interactionProof:{nonce, keystrokeCount, pasteEvents, totalTypingTime, startTime, submitTime},
           resend, aiRole, aiName, language}
    流式响应: SSE data:{"token":"..."} ... data:[DONE]
    非流式响应: {answer: "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi.responses import StreamingResponse

from . import config, formats
from .session import BrowserManager

logger = logging.getLogger(__name__)

TONE_MAP = {
    "friendly": "friendly",
    "concise": "concise",
    "detailed": "detailed",
    "academic": "academic",
    "professional": "professional",
    "humorous": "humorous",
}
FORMAT_MAP = {
    "paragraph": "paragraph",
    "bullets": "bullet points",
    "bullet_points": "bullet points",
    "step_by_step": "step-by-step",
    "steps": "step-by-step",
    "mixed": "mixed",
}


class ChatProvider:
    def __init__(self, session: BrowserManager) -> None:
        self.session = session
        # nonce 短缓存：省掉每次对话前的一次上游往返（见 config.NONCE_CACHE_TTL）
        self._nonce: str = ""
        self._nonce_ts: float = 0.0
        self._nonce_lock = asyncio.Lock()

    # ---------- 入口 ----------

    async def chat_completion(self, request_data: Dict[str, Any]) -> StreamingResponse:
        stream = bool(request_data.get("stream", False))
        model = request_data.get("model") or config.settings.DEFAULT_CHAT_MODEL
        request_id = f"chatcmpl-{formats.random_id(24)}"
        if stream:
            return StreamingResponse(
                self._stream_openai(request_data, model, request_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return StreamingResponse(
            self._nonstream_openai(request_data, model, request_id),
            media_type="application/json",
        )

    async def anthropic_messages(self, request_data: Dict[str, Any]) -> StreamingResponse:
        stream = bool(request_data.get("stream", False))
        model = request_data.get("model") or config.settings.DEFAULT_CHAT_MODEL
        request_id = f"msg_{formats.random_id(16)}"
        if stream:
            return StreamingResponse(
                self._stream_anthropic(request_data, model, request_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return StreamingResponse(
            self._nonstream_anthropic(request_data, model, request_id),
            media_type="application/json",
        )

    # ---------- 公共上游调用 ----------

    async def get_nonce(self) -> str:
        """获取上游 nonce（CDP 通道偶发超时，重试 3 次；TTL 内复用缓存）。"""
        async with self._nonce_lock:
            now = time.time()
            if self._nonce and now - self._nonce_ts < config.settings.NONCE_CACHE_TTL:
                return self._nonce
            for attempt in range(3):
                try:
                    res = await self.session.fetch(config.settings.nonce_api_url, method="GET", timeout=60.0)
                    if res.get("ok"):
                        data = json.loads(res.get("text", "{}"))
                        nonce = data.get("nonce", "")
                        if nonce:
                            self._nonce = nonce
                            self._nonce_ts = time.time()
                            return nonce
                except Exception as e:  # noqa: BLE001
                    logger.warning("获取 nonce 失败（第 %d 次）: %s", attempt + 1, e)
                await asyncio.sleep(2)
            logger.error("获取 nonce 连续失败 3 次")
            return ""

    def invalidate_nonce(self) -> None:
        """上游拒绝请求后作废缓存的 nonce（若是单次有效，下次自动取新的）。"""
        self._nonce = ""
        self._nonce_ts = 0.0

    def _endpoint_for(self, model: str) -> str:
        ep = config.settings.MODEL_ENDPOINTS.get(model)
        if not ep:
            # 尝试前缀匹配（如 model 带版本后缀）
            for mid, m_ep in config.settings.MODEL_ENDPOINTS.items():
                if model.startswith(mid):
                    ep = m_ep
                    break
        return ep or "/api/generate-ai-answer-foundry"

    def _tone_from(self, request_data: Dict[str, Any]) -> str:
        # OpenAI 没有 tone；Anthropic 也没有。默认 friendly；支持通过扩展字段 tone 透传
        tone = request_data.get("tone") or "friendly"
        return TONE_MAP.get(str(tone).lower(), "friendly")

    def _format_from(self, request_data: Dict[str, Any]) -> str:
        fmt = request_data.get("format") or "paragraph"
        return FORMAT_MAP.get(str(fmt).lower(), "paragraph")

    def _system_notice(self, system: Optional[str]) -> Optional[str]:
        return system

    async def _run_upstream(
        self,
        model: str,
        question: str,
        history: List[Dict[str, Any]],
        system: Optional[str],
        tone: str,
        fmt: str,
        stream: bool = False,
        request_data: Optional[Dict[str, Any]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """调用上游并产出解析后的 JSON 事件（token / error），流后以 {'__done__': True} 结束。"""
        nonce = await self.get_nonce()
        now_ms = int(time.time() * 1000)
        proof = {
            "nonce": nonce,
            "keystrokeCount": min(max(len(question) // 8, 2), 40),
            "pasteEvents": 0,
            "totalTypingTime": min(max(len(question) * 60, 500), 15000),
            "startTime": now_ms - min(max(len(question) * 60, 500), 15000) - random_jitter(),
            "submitTime": now_ms,
        }
        messages = history
        # 原生 tools 参数适配：上游不识别 tools → 渲染成 system prompt 协议段
        if request_data and request_data.get("tools"):
            tools_section = formats.tools_prompt(request_data.get("tools"), request_data.get("tool_choice"))
            if tools_section:
                system = (system or "") + tools_section
        if system:
            messages = [{"role": "system", "content": system}] + history
        # 上游只接受最近 20 条
        messages = messages[-20:]

        # 图片透传：把最后一条 user 消息里的图片塞进上游 file 字段（vision 能力由上游决定）
        image_ref = _extract_last_image(request_data or {})
        payload = {
            "model": model,
            "question": question,
            "tone": tone,
            "format": fmt,
            "file": image_ref,
            "conversationHistory": [
                {"role": "user" if m.get("role") == "system" else m.get("role", "user"),
                 "content": m.get("content", "")}
                for m in messages
                if isinstance(m.get("content"), str) and m.get("content")
            ],
            "interactionProof": proof,
            "resend": False,
            "aiRole": "assistant",
            "aiName": "",
            "language": "",
        }
        endpoint = self._endpoint_for(model)
        url = f"{config.settings.MAIN_ORIGIN}{endpoint}"
        logger.info("chat upstream: %s model=%s qlen=%d hist=%d", endpoint, model, len(question), len(messages))

        if stream:
            # 流式：浏览器内 fetch + reader 逐块回调（真流式，见 cdp_bridge.stream_chat）
            queue: "asyncio.Queue[Optional[str]]" = asyncio.Queue()

            async def on_data(chunk: str) -> None:
                await queue.put(chunk)

            async def on_end() -> None:
                await queue.put(None)

            async def stream_task() -> None:
                """浏览器侧流式任务：异常也要及时投递到队列，不能让消费端干等超时。"""
                try:
                    await self.session.stream_chat(
                        url, payload, on_data, on_end, timeout=config.settings.UPSTREAM_TIMEOUT
                    )
                except Exception as e:  # noqa: BLE001
                    logger.error("上游流式失败: %s", e)
                    self.invalidate_nonce()
                    try:
                        await queue.put({"type": "error", "message": f"上游流式失败: {e}"})
                    finally:
                        await queue.put(None)

            task = asyncio.create_task(stream_task())
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(
                            queue.get(), timeout=config.settings.UPSTREAM_TIMEOUT + 30
                        )
                    except asyncio.TimeoutError:
                        logger.error("上游流式超时")
                        break
                    if item is None:
                        break
                    if isinstance(item, dict):
                        yield item
                        continue
                    evt = _parse_event_line(item)
                    if evt is None:
                        # CDP 桥回调的是已剥壳的纯 JSON（如 {"token":"..."}），直接解析
                        try:
                            obj = json.loads(item)
                            if isinstance(obj, dict):
                                evt = obj
                        except Exception:  # noqa: BLE001
                            evt = None
                    if evt is not None:
                        yield evt
            finally:
                # 防止槽位锁泄漏：无论正常/超时都取消浏览器侧任务
                if not task.done():
                    task.cancel()
                try:
                    await asyncio.gather(task, return_exceptions=True)
                except Exception:  # noqa: BLE001
                    pass
            yield {"__done__": True}
            return

        # 非流式：浏览器内一次性 fetch
        try:
            res = await self.session.fetch(url, method="POST", body=payload, timeout=config.settings.UPSTREAM_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            self.invalidate_nonce()
            yield {"type": "error", "message": f"浏览器 fetch 失败: {e}"}
            yield {"__done__": True}
            return
        if not res.get("ok"):
            self.invalidate_nonce()
            status = res.get("status")
            body_text = res.get("text", "")[:300]
            detail = ""
            try:
                detail = json.loads(res.get("text", "{}")).get("error", "")
            except Exception:  # noqa: BLE001
                pass
            if status == 409 and "model_unavailable" in str(detail):
                yield {"type": "error", "message": "上游模型不可用（model_unavailable）"}
            else:
                yield {"type": "error", "message": f"上游 HTTP {status}: {detail or body_text}"}
            yield {"__done__": True}
            return
        evts = parse_event_lines(res.get("text", ""))
        if not evts:
            # JSON answer 格式（含 softBlock 容量阻断）
            try:
                body = json.loads(res.get("text", "{}"))
            except Exception:  # noqa: BLE001
                body = {}
            if body.get("softBlock"):
                yield {"type": "error", "message": "上游容量已满（capacity_block），请稍后重试"}
            else:
                answer = body.get("answer") or res.get("text", "").strip()
                yield {"token": answer}
        else:
            for evt in evts:
                if isinstance(evt, dict) and evt.get("status") == 409:
                    yield {"type": "error", "message": "上游模型不可用（model_unavailable）"}
                    yield {"__done__": True}
                    return
                yield evt
        yield {"__done__": True}

    # ---------- OpenAI 流式 ----------

    async def _stream_openai(self, request_data: Dict[str, Any], model: str, request_id: str) -> AsyncGenerator[bytes, None]:
        question, history = _prepare_turn(request_data)
        system = _extract_system(request_data)
        tone = self._tone_from(request_data)
        fmt = self._format_from(request_data)

        # 原生 tools 流式：收集完整输出后决定形态（tool_calls 事件序列 / 普通 token 分块）
        if request_data.get("tools"):
            buffer: list[str] = []
            error: Optional[str] = None
            try:
                async for evt in self._run_upstream(model, question, history, system, tone, fmt, stream=True, request_data=request_data):
                    if evt.get("__done__"):
                        break
                    tok = evt.get("token")
                    if tok:
                        buffer.append(tok)
                    elif evt.get("type") == "error" or "message" in evt:
                        error = str(evt.get("message") or evt.get("error") or evt)[:300]
            except Exception as e:  # noqa: BLE001
                error = str(e)
            content = "".join(buffer)
            logger.info("[tools-stream] 收集 %d chars, error=%s, head=%r",
                        len(content), (error or "")[:80], content[:120])
            tc = formats.parse_tool_call(content)
            if tc:
                for ch in formats.openai_stream_tool_chunks(request_id, model, tc["name"], tc["arguments"]):
                    yield formats.sse(ch).encode()
                yield formats.DONE.encode()
                return
            # 未命中工具协议：正常按 token 分块输出（含未闭合协议行时先清理，避免把标记吐给客户端）
            if formats.TOOL_MARKER in content:
                content = formats.strip_tool_marker(content)
            yield formats.sse(formats.openai_chunk(request_id, model, "")).encode()
            if error:
                yield formats.sse(formats.openai_chunk(request_id, model, f"\n[error] {error}", "stop")).encode()
            else:
                for piece in split_chunks(content, 48):
                    yield formats.sse(formats.openai_chunk(request_id, model, piece)).encode()
            yield formats.sse(formats.openai_chunk(request_id, model, "", "stop")).encode()
            yield formats.DONE.encode()
            return

        yield formats.sse(formats.openai_chunk(request_id, model, "")).encode()
        stopped = False
        try:
            async for evt in self._run_upstream(model, question, history, system, tone, fmt, stream=True, request_data=request_data):
                if evt.get("__done__"):
                    break
                tok = evt.get("token")
                if tok:
                    yield formats.sse(formats.openai_chunk(request_id, model, tok)).encode()
                    continue
                if evt.get("type") == "error" or "message" in evt:
                    msg = str(evt.get("message") or evt.get("error") or evt)[:300]
                    yield formats.sse(formats.openai_chunk(request_id, model, f"\n[error] {msg}", "stop")).encode()
                    stopped = True
        except Exception as e:  # noqa: BLE001
            logger.error("chat stream error: %s", e)
            if not stopped:
                yield formats.sse(formats.openai_chunk(request_id, model, f"\n[error] {e}", "stop")).encode()
                stopped = True
        if not stopped:
            yield formats.sse(formats.openai_chunk(request_id, model, "", "stop")).encode()
        yield formats.DONE.encode()

    # ---------- OpenAI 非流式 ----------

    async def _nonstream_openai(self, request_data: Dict[str, Any], model: str, request_id: str) -> AsyncGenerator[bytes, None]:
        question, history = _prepare_turn(request_data)
        system = _extract_system(request_data)
        tone = self._tone_from(request_data)
        fmt = self._format_from(request_data)

        buffer: list[str] = []
        error: Optional[str] = None
        try:
            async for evt in self._run_upstream(model, question, history, system, tone, fmt, stream=False, request_data=request_data):
                if evt.get("__done__"):
                    break
                tok = evt.get("token")
                if tok:
                    buffer.append(tok)
                elif evt.get("type") == "error" or "message" in evt:
                    error = str(evt.get("message") or evt.get("error") or evt)[:300]
        except Exception as e:  # noqa: BLE001
            error = str(e)
        content = "".join(buffer)
        # 原生 tools：模型输出命中协议 → 转成 OpenAI tool_calls；未命中但有残留协议行 → 清理
        tc = formats.parse_tool_call(content) if request_data.get("tools") else None
        if tc is None and formats.TOOL_MARKER in content:
            content = formats.strip_tool_marker(content)
        if tc:
            yield json.dumps(formats.openai_full_tool(request_id, model, tc["name"], tc["arguments"]), ensure_ascii=False).encode()
            return
        if error:
            content = f"{content}\n[error] {error}" if content else f"[error] {error}"
        yield json.dumps(formats.openai_full(request_id, model, content), ensure_ascii=False).encode()

    # ---------- Anthropic 流式 ----------

    async def _stream_anthropic(self, request_data: Dict[str, Any], model: str, request_id: str) -> AsyncGenerator[bytes, None]:
        question, history = _prepare_turn(request_data)
        system = _extract_system(request_data)
        tone = self._tone_from(request_data)
        fmt = self._format_from(request_data)

        # 原生 tools 流式：收集后输出规范 tool_use 事件序列
        if request_data.get("tools"):
            buffer: list[str] = []
            error: Optional[str] = None
            try:
                async for evt in self._run_upstream(model, question, history, system, tone, fmt, stream=True, request_data=request_data):
                    if evt.get("__done__"):
                        break
                    tok = evt.get("token")
                    if tok:
                        buffer.append(tok)
                    elif evt.get("type") == "error" or "message" in evt:
                        error = str(evt.get("message") or evt.get("error") or evt)[:300]
            except Exception as e:  # noqa: BLE001
                error = str(e)
            content = "".join(buffer)
            tc = formats.parse_tool_call(content)
            if tc:
                for evt in formats.anthropic_stream_tool_events(request_id, model, tc["name"], tc["arguments"]):
                    yield formats.sse(evt).encode()
                return
            # 未命中：正常文本流（含残留协议行时先清理）
            if formats.TOOL_MARKER in content:
                content = formats.strip_tool_marker(content)
            yield formats.anthropic_stream_start(request_id, model).encode()
            yield formats.anthropic_block_start().encode()
            yield formats.anthropic_delta(content + (f"\n[error] {error}" if error else "")).encode()
            yield formats.anthropic_block_stop().encode()
            yield formats.anthropic_stop().encode()
            return

        yield formats.anthropic_stream_start(request_id, model).encode()
        block_started = False
        try:
            async for evt in self._run_upstream(model, question, history, system, tone, fmt, stream=True, request_data=request_data):
                if evt.get("__done__"):
                    break
                tok = evt.get("token")
                if tok:
                    if not block_started:
                        yield formats.anthropic_block_start().encode()
                        block_started = True
                    yield formats.anthropic_delta(tok).encode()
                elif evt.get("type") == "error" or "message" in evt:
                    if not block_started:
                        yield formats.anthropic_block_start().encode()
                        block_started = True
                    yield formats.anthropic_delta(f"\n[error] {str(evt.get('message') or evt.get('error') or evt)[:300]}").encode()
        except Exception as e:  # noqa: BLE001
            logger.error("anthropic stream error: %s", e)
            if not block_started:
                yield formats.anthropic_block_start().encode()
                block_started = True
            yield formats.anthropic_delta(f"\n[error] {e}").encode()
        if block_started:
            yield formats.anthropic_block_stop().encode()
        yield formats.anthropic_stop().encode()

    # ---------- Anthropic 非流式 ----------

    async def _nonstream_anthropic(self, request_data: Dict[str, Any], model: str, request_id: str) -> AsyncGenerator[bytes, None]:
        question, history = _prepare_turn(request_data)
        system = _extract_system(request_data)
        tone = self._tone_from(request_data)
        fmt = self._format_from(request_data)

        buffer: list[str] = []
        error: Optional[str] = None
        try:
            async for evt in self._run_upstream(model, question, history, system, tone, fmt, stream=False, request_data=request_data):
                if evt.get("__done__"):
                    break
                tok = evt.get("token")
                if tok:
                    buffer.append(tok)
                elif evt.get("type") == "error" or "message" in evt:
                    error = str(evt.get("message") or evt.get("error") or evt)[:300]
        except Exception as e:  # noqa: BLE001
            error = str(e)
        content = "".join(buffer)
        # 原生 tools：转成 Anthropic tool_use block；未命中但有残留协议行 → 清理
        tc = formats.parse_tool_call(content) if request_data.get("tools") else None
        if tc is None and formats.TOOL_MARKER in content:
            content = formats.strip_tool_marker(content)
        if tc:
            yield json.dumps(formats.anthropic_full_tool(model, request_id, tc["name"], tc["arguments"]), ensure_ascii=False).encode()
            return
        if error:
            content = f"{content}\n[error] {error}" if content else f"[error] {error}"
        yield json.dumps(formats.anthropic_full(model, content, request_id), ensure_ascii=False).encode()


# ---------- 消息处理 ----------

def _extract_system(request_data: Dict[str, Any]) -> Optional[str]:
    # Anthropic 顶层 system
    sys_val = request_data.get("system")
    if isinstance(sys_val, str) and sys_val:
        return sys_val
    if isinstance(sys_val, list):
        texts = [b.get("text", "") for b in sys_val if isinstance(b, dict)]
        joined = "\n".join(t for t in texts if t)
        if joined:
            return joined
    # OpenAI 风格：messages 中的 role=system
    for m in request_data.get("messages", []):
        if m.get("role") == "system":
            return _content_to_text(m.get("content")) or None
    return None


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                t = item.get("type")
                if t in ("text", "input_text"):
                    parts.append(item.get("text", ""))
                elif t in ("image_url", "image"):
                    parts.append("[image]")
                elif t == "tool_use":
                    # Anthropic 工具调用块 → 文本占位（compact 后历史保真，模型仍知道调了什么）
                    try:
                        args = json.dumps(item.get("input", {}), ensure_ascii=False)
                    except Exception:  # noqa: BLE001
                        args = "{}"
                    parts.append(f"[tool_call {item.get('name', '')}({args})]")
                elif t == "tool_result":
                    rc = item.get("content", "")
                    rc = _content_to_text(rc) if isinstance(rc, list) else str(rc or "")
                    parts.append(f"[tool_result] {rc}")
                elif t == "thinking":
                    parts.append(item.get("thinking", ""))
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _tool_call_lines(m: Dict[str, Any]) -> List[str]:
    """把 assistant 消息级 tool_calls 序列化成模型可读的文本占位。"""
    lines = []
    for tc in m.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function", {}) if isinstance(tc.get("function"), dict) else {}
        args = fn.get("arguments", "")
        if not isinstance(args, str):
            try:
                args = json.dumps(args, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                args = "{}"
        lines.append(f"[tool_call {fn.get('name', '')}({args})]")
    return lines


def _hist_entry(m: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """把任意消息转成上游历史条目；无法转换返回 None。

    - assistant.tool_calls → 文本占位（agent compact 后历史保真）
    - role=tool（历史中段的工具结果）→ user 角色的 [tool_result] 文本
    """
    role = m.get("role", "user")
    if role == "system":
        return None
    text = _content_to_text(m.get("content"))
    if role == "assistant" and m.get("tool_calls"):
        lines = _tool_call_lines(m)
        text = "\n".join(([text] if text else []) + lines)
    if role == "tool":
        role = "user"
        text = f"[tool_result] {text}" if text else ""
    if not text:
        return None
    return {"role": role, "content": text}


def _prepare_turn(request_data: Dict[str, Any]) -> "tuple[str, List[Dict[str, str]]]":
    """切分本轮对话 → (question, history)。

    关键修复（agent 工具循环卡死第一轮的根因）：OpenAI 协议下 agent 回传工具结果时，
    消息末尾是连续的 role="tool" 消息 —— 它们就是本轮要上游处理的"提问"。
    旧实现只认 role="user"，工具结果被整段丢弃，模型拿不到结果、重复发起同一个
    工具调用，agent 框架因此停摆。这里把末尾 tool 结果（含并行调用多条）拼进
    question，并带上对应的 [tool_call] 上下文，与 tools_prompt 的续跑协议呼应。
    """
    msgs = [m for m in request_data.get("messages", []) if isinstance(m, dict) and m.get("role") != "system"]
    if not msgs:
        return "", []

    # 末尾连续的 tool 结果（并行工具调用会有多条）
    end = len(msgs)
    tool_results: List[Dict[str, Any]] = []
    while end > 0 and msgs[end - 1].get("role") == "tool":
        end -= 1
        tool_results.insert(0, msgs[end])

    if tool_results:
        parts: List[str] = []
        if end > 0 and msgs[end - 1].get("role") == "assistant":
            # 带上对应的 tool_call 上下文，模型才知道结果对应哪次调用
            parts.extend(_tool_call_lines(msgs[end - 1]))
            end -= 1
        for t in tool_results:
            txt = _content_to_text(t.get("content"))
            parts.append(f"[tool_result] {txt or '（工具返回空结果）'}")
        question = "\n".join(p for p in parts if p)
    else:
        last = msgs[-1]
        question = _content_to_text(last.get("content"))
        if not question and last.get("tool_calls"):
            # assistant 结尾且 content 为空（agent 中断场景）→ 保留调用占位
            question = "\n".join(_tool_call_lines(last))
        end = len(msgs) - 1  # 最后一条本身是 question，不计入历史

    history = [h for h in (_hist_entry(m) for m in msgs[:end]) if h]
    return question, history


def _extract_last_image(request_data: Dict[str, Any]) -> Optional[Any]:
    """取最后一条 user 消息中的图片，转换成上游 file 字段的**对象格式**。

    上游前端逆向确认的真实格式（ChatRedesignInterface.js）：
        file = { data: "<纯base64>", type: "image/png", name: "xxx.png" }
    （注意：不是 data URI 字符串 —— 传字符串会导致上游转换损坏、模型幻觉作答）

    - OpenAI: {"type":"image_url","image_url":{"url":...}}
    - Anthropic: {"type":"image","source":{"type":"base64","data":...}} / url source
    """
    for m in reversed(request_data.get("messages", [])):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                t = item.get("type")
                if t == "image_url":
                    url = item.get("image_url", {})
                    url = url.get("url", "") if isinstance(url, dict) else str(url)
                    if not url:
                        continue
                    if url.startswith("data:") and ";base64," in url:
                        head, b64 = url.split(";base64,", 1)
                        mime = head[5:] or "image/png"
                        ext = mime.split("/")[-1].replace("jpeg", "jpg")
                        return {"data": b64, "type": mime, "name": f"image.{ext}"}
                    # http(s) URL：原样透传（上游接受 URL 时可直接用）
                    return url
                elif t == "image":
                    src = item.get("source", {}) or {}
                    if src.get("type") == "base64" and src.get("data"):
                        mime = src.get("media_type", "image/png")
                        ext = mime.split("/")[-1].replace("jpeg", "jpg")
                        return {"data": src["data"], "type": mime, "name": f"image.{ext}"}
                    if src.get("type") == "url" and src.get("url"):
                        return src["url"]
        return None  # 只检查最近一条 user 消息
    return None


def _parse_event_line(line: str) -> Optional[Dict[str, Any]]:
    """把上游 SSE 数据行解析为事件 dict；无法解析返回 None。"""
    for raw in line.splitlines():
        raw = raw.strip()
        if not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            return None
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict):
            return evt
    return None


def parse_event_lines(text: str) -> List[Dict[str, Any]]:
    """把完整上游响应体解析为事件列表（支持多行 SSE / 单条 JSON）。"""
    out: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            evt = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict):
            out.append(evt)
    return out


def split_chunks(text: str, size: int = 24) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


def random_jitter() -> int:
    import random

    return random.randint(0, 400)