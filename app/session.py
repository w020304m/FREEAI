"""浏览器会话管理（主传输通道）：Playwright 持久化上下文 + 人机验证绕过 + 站内 fetch。

架构（实测验证）：
- 站点 aifreeforever.com 处于 Cloudflare 挑战之后（cf_clearance / __cf_bm / cf_chl_rc_ni）。
- 直连 HTTP 复用浏览器 cookie 不可靠（cf_clearance 与浏览器 TLS 指纹绑定，httpx 会被 403）。
- 可靠做法（与 Aifreeforever-2api 一致）：让"已通过挑战"的浏览器页面在站内执行 fetch，
  自动携带完整会话/指纹 → 实测 /api/* 全部 200。
- Cloudflare 挑战可能周期性复发（约数分钟到数小时不等），因此本管理器：
  * 维护 N 个持久化上下文（默认 2），每个上下文一把锁互斥；
  * 轮询调度（round-robin）避免单点；
  * 请求若命中 403 挑战 → 标记该槽失效 → 触发重新验证 → 自动重试；
  * 后台 keepalive 定期探测，提前发现会话过期。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

logger = logging.getLogger(__name__)

CHALLENGE_MARKERS = ("Just a moment", "Ray ID", "安全验证", "请稍候", "checking your browser", "cf_chl")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

KEEPALIVE_INTERVAL = 150  # 秒（过于频繁的 API 探测会触发 Cloudflare 反爬标记）
KEEPALIVE_FAIL_THRESHOLD = 2  # 连续失败 N 次才判定会话失效并重新验证


def looks_like_challenge(text: str, title: str = "") -> bool:
    t = f"{text} {title}"
    return any(m.lower() in t.lower() for m in CHALLENGE_MARKERS)


class SessionError(RuntimeError):
    """会话失效（命中挑战页）——调用方可据此重试。"""


class _Slot:
    __slots__ = ("context", "page", "lock", "ready", "last_used", "verify_task")

    def __init__(self, context: BrowserContext, page: Page) -> None:
        self.context = context
        self.page = page
        self.lock = asyncio.Lock()
        self.ready = False
        self.last_used = 0.0
        self.verify_task: Optional[asyncio.Task] = None


class BrowserManager:
    def __init__(
        self,
        chat_page: str,
        user_data_dir: str,
        contexts: int = 2,
        executable_path: str = "",
        headless: bool = False,
    ) -> None:
        self.chat_page = chat_page
        self.user_data_dir = user_data_dir
        self.contexts_count = max(1, contexts)
        self.executable_path = executable_path
        self.headless = headless
        self._pw = None
        self._slots: List[_Slot] = []
        self._ready = False
        self._rr = 0  # round-robin 游标
        self._verify_lock = asyncio.Lock()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._cookies: List[Dict[str, Any]] = []
        self._cookie_file = os.path.join(user_data_dir, "cookies.json")

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        self._load_cookies()
        self._pw = await async_playwright().start()
        os.makedirs(self.user_data_dir, exist_ok=True)
        for i in range(self.contexts_count):
            slot_dir = os.path.join(self.user_data_dir, f"ctx-{i}")
            os.makedirs(slot_dir, exist_ok=True)
            try:
                slot = await self._launch_slot(slot_dir)
                self._slots.append(slot)
                logger.info("browser context #%d launched", i)
            except Exception as e:  # noqa: BLE001
                logger.error("browser context #%d launch failed: %s", i, e)
        if self._slots:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    # ---------- cookie 管理（API 直连通道的核心）----------

    def _load_cookies(self) -> None:
        """启动时从 browser_data/cookies.json 加载 cookie（供 API 直连使用）。"""
        try:
            if os.path.exists(self._cookie_file):
                with open(self._cookie_file, "r", encoding="utf-8") as f:
                    self._cookies = json.load(f)
                logger.info("已加载持久化 cookie %d 条", len(self._cookies))
        except Exception as e:  # noqa: BLE001
            logger.warning("cookie 加载失败: %s", e)

    def cookies_obj(self) -> Dict[str, str]:
        """返回 name->value 字典（供 curl_cffi Cookie 头使用）。"""
        out: Dict[str, str] = {}
        for c in self._cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value:
                out[name] = value
            # 若已是 {name: value} 扁平字典
            elif isinstance(c, dict) and len(c) == 1:
                for k, v in c.items():
                    out[k] = v
        return out

    def import_cookies(self, cookies: List[Dict[str, Any]]) -> int:
        """导入用户浏览器手动过验证导出的 cookie（或求解服务提供的）。"""
        before = len(self._cookies)
        # 去重：同名同域保留后导入
        dedup: Dict[str, Dict[str, Any]] = {}
        for c in self._cookies:
            dedup[(c.get("name", ""), c.get("domain", ""), c.get("path", ""))] = c
        for c in cookies:
            if isinstance(c, dict) and c.get("name"):
                dedup[(c.get("name", ""), c.get("domain", ""), c.get("path", ""))] = c
        self._cookies = list(dedup.values())
        self._save_cookies()
        logger.info("导入 cookie %d 条（原有 %d → 现 %d）", len(cookies), before, len(self._cookies))
        return len(self._cookies)

    def _save_cookies(self) -> None:
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self._cookie_file, "w", encoding="utf-8") as f:
                json.dump(self._cookies, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning("cookie 保存失败: %s", e)

    async def _launch_slot(self, user_data_dir: str) -> _Slot:
        launch_kwargs: Dict[str, Any] = dict(
            user_data_dir=user_data_dir,
            headless=self.headless,
            user_agent=USER_AGENT,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--disable-extensions",
            ],
            ignore_https_errors=True,
            viewport={"width": 1440, "height": 900},
        )
        if self.executable_path and os.path.exists(self.executable_path):
            launch_kwargs["executable_path"] = self.executable_path
        else:
            for cand in (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            ):
                if os.path.exists(cand):
                    launch_kwargs["executable_path"] = cand
                    break
        context = await self._pw.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else await context.new_page()
        page.set_default_timeout(60000)
        try:
            from playwright_stealth import stealth_async

            await stealth_async(page)
        except Exception as e:  # noqa: BLE001
            logger.info("stealth 不可用（忽略）: %s", e)
        return _Slot(context, page)

    # ---------- 就绪 / 验证 ----------

    async def ensure_ready(self, timeout: float = 360.0) -> bool:
        """确保至少一个上下文已通过人机验证。"""
        if self._ready and any(s.ready for s in self._slots):
            return True
        return await self._verify_all(timeout)

    async def _verify_all(self, timeout: float = 360.0) -> bool:
        async with self._verify_lock:
            if any(s.ready for s in self._slots):
                self._ready = True
                return True
            deadline = time.time() + timeout
            tasks = [asyncio.create_task(self._verify_slot(s, i)) for i, s in enumerate(self._slots)]
            while time.time() < deadline:
                if any(s.ready for s in self._slots):
                    self._ready = True
                    logger.info("浏览器会话已就绪（%d 个上下文）", sum(1 for s in self._slots if s.ready))
                    return True
                await asyncio.sleep(3)
            for t in tasks:
                t.cancel()
            logger.error("等待人机验证超时")
            return False

    def _schedule_verify(self, slot: _Slot, index: int) -> None:
        """每槽单飞：已有进行中的验证任务则复用，避免 keepalive 与请求恢复并发导航。"""
        if slot.verify_task and not slot.verify_task.done():
            return
        slot.ready = False
        slot.verify_task = asyncio.create_task(self._verify_slot(slot, index))

    async def _verify_slot(self, slot: _Slot, index: int, attempt: int = 0) -> None:
        try:
            logger.info("[ctx%d] 打开 %s ...", index, self.chat_page)
            await slot.page.goto(self.chat_page, wait_until="domcontentloaded", timeout=60000)
            for i in range(36):  # 最长 ~3 分钟
                await asyncio.sleep(5)
                try:
                    title = await slot.page.title()
                    text = await slot.page.evaluate("document.body ? document.body.innerText.slice(0,400) : ''")
                except Exception:  # noqa: BLE001
                    continue
                if not looks_like_challenge(text, title):
                    await self._try_click_turnstile(slot)
                    await asyncio.sleep(2)
                    try:
                        probe = await slot.page.evaluate(
                            """async () => { const r = await fetch('/api/chat-models');
                                const t = await r.text(); return {status: r.status, head: t.slice(0,80)}; }"""
                        )
                        if probe.get("status") == 200:
                            logger.info("[ctx%d] 验证通过且 API 可达 (title=%r)", index, title)
                            slot.ready = True
                            slot.last_used = time.time()
                            return
                        logger.warning("[ctx%d] 挑战通过但 API 返回 HTTP %s", index, probe.get("status"))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[ctx%d] API 探测异常: %s", index, e)
                else:
                    await self._try_click_turnstile(slot)
            # 超时：可能是陈旧 cookie 导致挑战卡死 → 清空 profile 重新启动一次
            if attempt == 0:
                logger.warning("[ctx%d] 验证超时，清空 profile 后重试", index)
                await self._wipe_and_relaunch(slot, index)
                await self._verify_slot(slot, index, attempt=1)
            else:
                logger.error("[ctx%d] 清空 profile 后仍验证失败", index)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("[ctx%d] 验证异常: %s", index, e)

    async def _wipe_and_relaunch(self, slot: _Slot, index: int) -> None:
        try:
            await slot.context.close()
        except Exception:  # noqa: BLE001
            pass
        slot_dir = os.path.join(self.user_data_dir, f"ctx-{index}")
        try:
            import shutil

            if os.path.exists(slot_dir):
                shutil.rmtree(slot_dir, ignore_errors=True)
            os.makedirs(slot_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[ctx%d] 清理 profile 失败: %s", index, e)
        try:
            new_slot = await self._launch_slot(slot_dir)
            slot.context = new_slot.context
            slot.page = new_slot.page
            slot.ready = False
            slot.lock = asyncio.Lock()
        except Exception as e:  # noqa: BLE001
            logger.error("[ctx%d] 重新启动浏览器失败: %s", index, e)

    async def _try_click_turnstile(self, slot: _Slot) -> None:
        try:
            for f in slot.page.frames:
                if "challenges.cloudflare.com" in f.url:
                    box = f.locator("input[type=checkbox], .ctp-checkbox-label, .cb-lb")
                    if await box.count() > 0:
                        await box.first.click(timeout=2000)
                        logger.info("已点击 Cloudflare Turnstile 复选框")
        except Exception:  # noqa: BLE001
            pass

    async def _keepalive_loop(self) -> None:
        """低频探测各就绪槽位；连续失败 N 次才重新验证，避免自身触发反爬。"""
        fail_counts: Dict[int, int] = {}
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            for i, slot in enumerate(self._slots):
                if not slot.ready:
                    fail_counts.pop(i, None)
                    continue
                try:
                    async with slot.lock:
                        probe = await slot.page.evaluate(
                            """async () => { const r = await fetch('/api/chat-models');
                                const t = await r.text(); return {status: r.status, head: t.slice(0,60)}; }"""
                        )
                    if probe.get("status") == 200:
                        slot.last_used = time.time()
                        fail_counts.pop(i, None)
                    else:
                        fail_counts[i] = fail_counts.get(i, 0) + 1
                        logger.warning(
                            "[ctx%d] keepalive 探测 HTTP %s（连续 %d 次失败）",
                            i, probe.get("status"), fail_counts[i],
                        )
                        if fail_counts[i] >= KEEPALIVE_FAIL_THRESHOLD:
                            fail_counts.pop(i, None)
                            self._schedule_verify(slot, i)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[ctx%d] keepalive 异常: %s", i, e)
                    fail_counts[i] = fail_counts.get(i, 0) + 1
                    if fail_counts[i] >= KEEPALIVE_FAIL_THRESHOLD:
                        fail_counts.pop(i, None)
                        self._schedule_verify(slot, i)

    async def _quick_recover(self, slot: _Slot, index: int, wait: float = 40.0) -> bool:
        """快速恢复：重新加载聊天页，短等挑战自动通过，探测 API 可达即恢复。"""
        try:
            async with slot.lock:
                await slot.page.goto(self.chat_page, wait_until="domcontentloaded", timeout=45000)
                deadline = time.time() + wait
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    await self._try_click_turnstile(slot)
                    try:
                        probe = await slot.page.evaluate(
                            """async () => { const r = await fetch('/api/chat-models');
                                const t = await r.text(); return {status: r.status, head: t.slice(0,60)}; }"""
                        )
                        if probe.get("status") == 200:
                            slot.ready = True
                            slot.last_used = time.time()
                            logger.info("[ctx%d] 快速恢复成功", index)
                            return True
                    except Exception:  # noqa: BLE001
                        continue
            logger.warning("[ctx%d] 快速恢复失败（%ss 内挑战未通过）", index, wait)
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("[ctx%d] 快速恢复异常: %s", index, e)
            return False

    # ---------- 槽位选择 ----------

    def _pick_slot(self) -> Optional[_Slot]:
        ready_slots = [s for s in self._slots if s.ready]
        if not ready_slots:
            return None
        # round-robin
        slot = ready_slots[self._rr % len(ready_slots)]
        self._rr += 1
        return slot

    @property
    def is_ready(self) -> bool:
        # 直连通道就绪 = 有可用 cookie；浏览器通道就绪 = 至少一个槽验证通过
        from . import config as _cfg

        try:
            if _cfg.settings.USE_DIRECT_HTTP:
                return bool(self._cookies)
        except Exception:  # noqa: BLE001
            pass
        return any(s.ready for s in self._slots)

    async def request_probe(self) -> str:
        """直连探测 /api/chat-models 是否可达，返回 'HTTP 200' / 'HTTP xxx' / 'error'。"""
        try:
            from . import config as _cfg

            res = await self._direct_fetch(_cfg.settings.models_api_url, "GET", timeout=30.0)
            return f"HTTP {res.get('status')}"
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    # ---------- 站内请求 ----------

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """请求上游：优先 curl_cffi API 直连（Chrome TLS 指纹 + cookie），403 挑战则回退浏览器。

        架构（用户方向修正）：反代的是站内 API，业务主通道应为直连；
        浏览器只用于"获取/刷新 cookie"（人工过一次验证或外部求解服务）。
        """
        from . import config as _cfg

        direct_ok = False
        try:
            direct_ok = bool(
                _cfg.settings.USE_DIRECT_HTTP
                and _cfg.settings.MAIN_ORIGIN in url
                and self._cookies
            )
        except Exception:  # noqa: BLE001
            direct_ok = False

        if direct_ok:
            for attempt in range(retries + 1):
                try:
                    res = await self._direct_fetch(url, method, body, headers, timeout)
                    if res.get("status") in (401, 403) and looks_like_challenge(res.get("text", "")):
                        logger.warning("直连命中挑战（HTTP %s），尝试浏览器通道", res.get("status"))
                        return await self._browser_fetch(url, method, body, headers, timeout, retries)
                    return res
                except Exception as e:  # noqa: BLE001
                    logger.warning("直连请求异常（第 %d 次）: %s", attempt + 1, e)
                    await asyncio.sleep(1)
            raise SessionError(f"直连请求失败（请检查 cookie 是否有效）")
        return await self._browser_fetch(url, method, body, headers, timeout, retries)

    async def _browser_fetch(
        self,
        url: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """页面上下文内 fetch（备用通道）。命中挑战页时标记失效并重试。"""
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            slot = self._pick_slot()
            if slot is None:
                ok = await self.ensure_ready(timeout=30.0)
                if not ok:
                    raise SessionError("没有已通过验证的浏览器会话")
                slot = self._pick_slot()
                if slot is None:
                    raise SessionError("没有可用浏览器会话")
            try:
                res = await self._fetch_on(slot, url, method, body, headers, timeout)
                if res.get("status") == 403 and looks_like_challenge(res.get("text", "")):
                    raise SessionError(f"会话失效（HTTP 403 挑战），第 {attempt + 1} 次重试")
                return res
            except SessionError as e:
                last_err = e
                logger.warning("请求 %s 命中挑战，尝试恢复会话", url[:80])
                slot.ready = False
                self._ready = any(s.ready for s in self._slots)
                # 快速恢复：重载页面 + 短等（挑战通常 10-30s 内自动通过）
                recovered = await self._quick_recover(slot, index=self._slots.index(slot))
                if not recovered:
                    self._schedule_verify(slot, self._slots.index(slot))
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("请求 %s 异常（第 %d 次）: %s", url[:80], attempt + 1, e)
                await asyncio.sleep(2)
        raise SessionError(f"请求失败: {last_err}")

    async def _direct_fetch(
        self,
        url: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        """curl_cffi API 直连：模拟 Chrome TLS 指纹 + 携带当前 cookie。

        返回 {ok, status, text}，与浏览器 fetch 形状一致。
        """
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as e:  # noqa: BLE001
            raise SessionError(f"curl_cffi 未安装: {e}")

        jar = self.cookies_obj()
        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://aifreeforever.com",
            "Referer": "https://aifreeforever.com/chat/gpt-5-mini",
            "x-api-secret": "",
        }
        if headers:
            req_headers.update(headers)

        cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items()) if hasattr(jar, "items") else ""
        if not cookie_str:
            try:
                cookie_str = "; ".join(
                    f"{c['name']}={c['value']}" for c in self._cookies if c.get("name") and c.get("value")
                )
            except Exception:  # noqa: BLE001
                cookie_str = ""
        req_headers["Cookie"] = cookie_str

        async with AsyncSession(impersonate="chrome", timeout=timeout) as s:
            if method.upper() == "GET":
                r = await s.get(url, headers=req_headers)
            else:
                r = await s.post(url, json=body or {}, headers=req_headers)
            text = r.text[:200000]
            return {"ok": r.status_code == 200, "status": r.status_code, "text": text}

    async def _fetch_on(
        self,
        slot: _Slot,
        url: str,
        method: str,
        body: Optional[Dict[str, Any]],
        headers: Optional[Dict[str, str]],
        timeout: float,
    ) -> Dict[str, Any]:
        body_json = json.dumps(body) if body is not None else None
        headers_json = json.dumps(headers or {})
        js = f"""async () => {{
            const ctrl = new AbortController();
            const t = setTimeout(() => ctrl.abort(), {int(timeout * 1000)});
            try {{
                const r = await fetch('{url}', {{
                    method: '{method}',
                    headers: Object.assign({{'Content-Type': 'application/json', 'Accept': '*/*'}}, {headers_json}),
                    body: {("JSON.stringify(" + body_json + ")") if body_json is not None else 'undefined'},
                    signal: ctrl.signal
                }});
                const text = await r.text();
                return {{ok: r.ok, status: r.status, text: text.slice(0, 200000)}};
            }} finally {{ clearTimeout(t); }}
        }}"""
        async with slot.lock:
            slot.last_used = time.time()
            return await slot.page.evaluate(js)

    async def fetch_multipart(
        self,
        url: str,
        field: str,
        filename: str,
        b64_content: str,
        mime: str = "image/png",
        retries: int = 2,
    ) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            slot = self._pick_slot()
            if slot is None:
                await self.ensure_ready(timeout=30.0)
                slot = self._pick_slot()
                if slot is None:
                    raise SessionError("没有可用浏览器会话")
            try:
                js = f"""async () => {{
                    const binary = atob('{b64_content}');
                    const bytes = new Uint8Array(binary.length);
                    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
                    const blob = new Blob([bytes], {{type: '{mime}'}});
                    const fd = new FormData();
                    fd.append('{field}', blob, '{filename}');
                    const r = await fetch('{url}', {{method: 'POST', body: fd}});
                    const text = await r.text();
                    return {{ok: r.ok, status: r.status, text: text.slice(0, 2000)}};
                }}"""
                async with slot.lock:
                    slot.last_used = time.time()
                    res = await slot.page.evaluate(js)
                if res.get("status") == 403 and looks_like_challenge(res.get("text", "")):
                    raise SessionError("会话失效（HTTP 403 挑战）")
                return res
            except SessionError as e:
                last_err = e
                slot.ready = False
                self._schedule_verify(slot, self._slots.index(slot))
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001
                last_err = e
                await asyncio.sleep(2)
        raise SessionError(f"multipart 请求失败: {last_err}")

    async def stream_chat(
        self,
        url: str,
        payload: Dict[str, Any],
        on_data: Callable[[str], Any],
        on_end: Callable[[], Any],
        retries: int = 2,
        timeout: float = 300.0,
    ) -> None:
        """聊天流式：优先 curl_cffi 直连（SSE 逐行回调），403 挑战回退浏览器内 fetch。"""
        from . import config as _cfg

        direct_ok = False
        try:
            direct_ok = bool(_cfg.settings.USE_DIRECT_HTTP and self._cookies)
        except Exception:  # noqa: BLE001
            direct_ok = False

        if direct_ok:
            for attempt in range(retries + 1):
                try:
                    await self._direct_stream(url, payload, on_data, on_end, timeout)
                    return
                except SessionError as e:
                    if attempt >= retries:
                        raise
                    await asyncio.sleep(2)
                except Exception as e:  # noqa: BLE001
                    logger.warning("直连流式异常（第 %d 次）: %s", attempt + 1, e)
                    await asyncio.sleep(2)
            # 直连全失败 → 回退浏览器
        await self._browser_stream(url, payload, on_data, on_end, retries, timeout)

    async def _direct_stream(
        self,
        url: str,
        payload: Dict[str, Any],
        on_data: Callable[[str], Any],
        on_end: Callable[[], Any],
        timeout: float = 300.0,
    ) -> None:
        """curl_cffi 流式直连：POST + 逐行解析 SSE data: 事件回调。"""
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as e:  # noqa: BLE001
            raise SessionError(f"curl_cffi 未安装: {e}")

        cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies_obj().items())
        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://aifreeforever.com",
            "Referer": "https://aifreeforever.com/chat/gpt-5-mini",
            "Cookie": cookie_str,
            "Content-Type": "application/json",
            "x-api-secret": "",
        }
        async with AsyncSession(impersonate="chrome", timeout=timeout) as s:
            async with s.stream("POST", url, json=payload, headers=req_headers) as r:
                if r.status_code not in (200, 201):
                    body = (await r.aread())[:300].decode("utf-8", "ignore")
                    raise SessionError(f"直连流式 HTTP {r.status_code}: {body}")
                async for line in r.aiter_lines():
                    line = (line or "").strip()
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data and data != "[DONE]":
                            await on_data(data)
        await on_end()

    async def _browser_stream(
        self,
        url: str,
        payload: Dict[str, Any],
        on_data: Callable[[str], Any],
        on_end: Callable[[], Any],
        retries: int = 2,
        timeout: float = 300.0,
    ) -> None:
        """页面内流式 fetch（备用）；命中挑战页时标记失效并重试。"""
        import secrets

        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            slot = self._pick_slot()
            if slot is None:
                await self.ensure_ready(timeout=30.0)
                slot = self._pick_slot()
                if slot is None:
                    raise SessionError("没有可用浏览器会话")
            page = slot.page
            payload_json = json.dumps(payload)
            suffix = secrets.token_hex(6)
            chunk_fn = f"onStreamChunk_{suffix}"
            end_fn = f"onStreamEnd_{suffix}"
            first_status: Dict[str, Any] = {"code": None}

            async def _safe_chunk(chunk: str) -> None:
                # 首事件可能是错误（含 status）
                try:
                    evt = json.loads(chunk)
                    if isinstance(evt, dict) and evt.get("status") and first_status["code"] is None:
                        first_status["code"] = evt.get("status")
                except Exception:  # noqa: BLE001
                    pass
                await on_data(chunk)

            js = f"""
            async () => {{
                const ctrl = new AbortController();
                const t = setTimeout(() => ctrl.abort(), {int(timeout * 1000)});
                try {{
                    const response = await fetch('{url}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json', 'Accept': '*/*'}},
                        body: JSON.stringify({payload_json}),
                        signal: ctrl.signal
                    }});
                    if (!response.ok) {{
                        const errorBody = await response.text();
                        await window.{chunk_fn}(JSON.stringify({{type: 'error', status: response.status, message: errorBody.slice(0, 300)}}));
                        return;
                    }}
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    while (true) {{
                        const {{ done, value }} = await reader.read();
                        if (done) {{
                            if (buffer && buffer.startsWith('data:')) {{
                                const data = buffer.substring(6).trim();
                                if (data && data !== '[DONE]') await window.{chunk_fn}(data);
                            }}
                            break;
                        }}
                        buffer += decoder.decode(value, {{stream: true}});
                        let nl;
                        while ((nl = buffer.indexOf('\\n')) >= 0) {{
                            const line = buffer.substring(0, nl);
                            buffer = buffer.substring(nl + 1);
                            if (line.trim() === '') continue;
                            if (line.startsWith('data:')) {{
                                const data = line.substring(6).trim();
                                if (data && data !== '[DONE]') await window.{chunk_fn}(data);
                            }}
                        }}
                    }}
                }} catch (e) {{
                    await window.{chunk_fn}(JSON.stringify({{type: 'error', message: String(e), aborted: e.name === 'AbortError'}}));
                }} finally {{
                    clearTimeout(t);
                    await window.{end_fn}();
                }}
            }}
            """
            try:
                async with slot.lock:
                    slot.last_used = time.time()
                    await page.expose_function(chunk_fn, _safe_chunk)
                    await page.expose_function(end_fn, lambda: on_end())
                    await page.evaluate(js)
                # 首事件判定：403 挑战则重试
                if first_status["code"] == 403:
                    raise SessionError("流式请求命中挑战页")
                return
            except SessionError as e:
                last_err = e
                slot.ready = False
                self._ready = any(s.ready for s in self._slots)
                self._schedule_verify(slot, self._slots.index(slot))
                await asyncio.sleep(3)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("流式请求异常（第 %d 次）: %s", attempt + 1, e)
                await asyncio.sleep(2)
            finally:
                try:
                    await page.expose_function(chunk_fn, lambda _: None)
                    await page.expose_function(end_fn, lambda: None)
                except Exception:  # noqa: BLE001
                    pass
        raise SessionError(f"流式请求失败: {last_err}")

    @property
    def _challenge_hit(self) -> bool:
        return False  # 兼容占位（废弃，流式判定已迁移到 stream_chat 内部）

    async def shutdown(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
        for slot in self._slots:
            try:
                await slot.context.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            await self._pw.stop()
        logger.info("浏览器管理器已关闭")
