"""CDP 会话桥：通过 CDP 只读附加到"用户手动过验证的干净 Edge 窗口"，在窗口内执行 API fetch。

背景（实测结论）：
- 用户手动过验证的真实浏览器会话 → 页面内 fetch /api/* 全部 200（唯一可靠通道）。
- Playwright/Selenium/uc 自动化（CDP 注入）→ Turnstile 判可疑 → 403。
- httpx/curl_cffi 直连（任何 TLS 指纹）→ cf_clearance 绑定浏览器指纹 → 403。

因此网关的"会话后端" = 干净 Edge 窗口（subprocess 启动或用户自行打开，`--remote-debugging-port`
+ `--remote-allow-origins=*`），本模块通过 CDP 只读接口（Runtime.evaluate / Network.getAllCookies）
执行站内 fetch，不注入任何自动化代码。

配套工具：tools/harvest_zero.py（自动拉起干净窗口）、tools/verify_inpage.py（验证）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 常见浏览器安装位置（按优先级）。用户可用 .env 的 BROWSER_EXECUTABLE 覆盖。
_BROWSER_CANDIDATES = [
    # Microsoft Edge
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # Google Chrome
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
# 兜底：按可执行文件名在 PATH 中查找
_BROWSER_NAMES = ["msedge.exe", "msedge", "chrome.exe", "chrome"]


def find_browser() -> str:
    """定位可用的浏览器可执行文件。

    优先级：.env 的 BROWSER_EXECUTABLE > 常见安装路径 > PATH 查找。
    找不到时返回默认 Edge 路径（调用方会在启动失败时给出明确提示）。
    """
    # 1) .env 显式配置
    try:
        from . import config as _cfg

        configured = (getattr(_cfg.settings, "BROWSER_EXECUTABLE", "") or "").strip().strip('"')
        if configured:
            if os.path.isfile(configured):
                return configured
            logger.warning("BROWSER_EXECUTABLE 指向的文件不存在：%s（回退到自动探测）", configured)
    except Exception:  # noqa: BLE001
        pass

    # 2) 常见安装路径
    for path in _BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path

    # 3) PATH 查找
    import shutil

    for name in _BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found

    logger.warning(
        "未找到 Edge/Chrome 可执行文件，将使用默认路径 %s；"
        "若启动失败请在 .env 中设置 BROWSER_EXECUTABLE 为你的浏览器完整路径",
        _BROWSER_CANDIDATES[0],
    )
    return _BROWSER_CANDIDATES[0]


EDGE = _BROWSER_CANDIDATES[0]  # 兼容旧引用


def looks_like_challenge(text: str, title: str = "") -> bool:
    t = f"{text} {title}"
    return any(m in t for m in ("Just a moment", "Ray ID", "安全验证", "请稍候"))


def _probe_status(probe: Any) -> int:
    """从探测返回值中提取 HTTP 状态码（0 = 解析失败/-1 = 网络异常）。"""
    try:
        obj = json.loads(probe) if isinstance(probe, str) else probe
        if isinstance(obj, dict) and "status" in obj:
            return int(obj["status"])
    except Exception:  # noqa: BLE001
        pass
    return 0


class CdpBridge:
    """附加到运行中的 Edge CDP 端口，只读执行页面内 fetch。"""

    def __init__(self, port: int = 9230, chat_page: str = "https://aifreeforever.com/chat/gpt-5-mini") -> None:
        self.port = port
        self.chat_page = chat_page
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self._cookies: List[Dict[str, Any]] = []
        self._cookie_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "browser_data", "cdp_cookies.json"
        )

    # ---------- cookie（供 /admin/cookies 兼容；CDP 模式下主要靠窗口会话）----------

    def import_cookies(self, cookies: List[Dict[str, Any]]) -> int:
        self._cookies = cookies
        try:
            os.makedirs(os.path.dirname(self._cookie_file), exist_ok=True)
            with open(self._cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning("cdp cookie 保存失败: %s", e)
        logger.info("CDP 桥导入 cookie %d 条", len(cookies))
        return len(cookies)

    async def request_probe(self) -> str:
        """直连探测 /api/chat-models（CDP 模式下通过窗口内 fetch）。"""
        try:
            res = await self.fetch("https://aifreeforever.com/api/chat-models", "GET", timeout=30.0)
            return f"HTTP {res.get('status')}"
        except Exception as e:  # noqa: BLE001
            return f"error: {e}"

    # ---------- 生命周期 ----------

    def launch(self) -> None:
        """若 CDP 端口无响应，则启动一个干净 Edge 窗口（零自动化注入）。"""
        if self.is_alive():
            logger.info("检测到 CDP 端口 %d 有响应，附加到已有窗口（是否已过验证由 ensure_ready 判定）", self.port)
            return
        from . import config as _cfg

        profile = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "browser_data", "cdp_profile"
        )
        os.makedirs(profile, exist_ok=True)
        browser = find_browser()
        if not os.path.isfile(browser):
            raise RuntimeError(
                f"未找到可用的浏览器（尝试路径：{browser}）。\n"
                "请安装 Microsoft Edge 或 Google Chrome，"
                "并在 .env 中设置 BROWSER_EXECUTABLE=<浏览器可执行文件完整路径>"
            )
        logger.info("使用浏览器：%s", browser)
        args = [
            browser,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={profile}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        # 出口代理（会话绑代理出口 IP：住宅代理可显著延长 cf_clearance 有效期 + 每 IP 图像配额按出口计）
        proxy = getattr(_cfg.settings, "OUTBOUND_PROXY", "") or ""
        if proxy:
            args.append(f"--proxy-server={proxy}")
            logger.info("窗口挂载出口代理: %s", proxy)
        args.append(self.chat_page)
        logger.info("启动干净 Edge 窗口（零自动化注入，端口 %d）...", self.port)
        try:
            self._proc = subprocess.Popen(args)
        except FileNotFoundError:
            logger.error("找不到 Edge: %s", EDGE)

    def is_alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _pick_slot(self) -> None:
        """CDP 模式下无 Playwright 槽位（供 turnstile.py 兼容判断）。"""
        return None

    def _list_pages(self) -> List[Dict[str, Any]]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5) as r:
                targets = json.loads(r.read())
            return [t for t in targets if t.get("type") == "page"]
        except Exception as e:  # noqa: BLE001
            logger.warning("CDP 列表获取失败: %s", e)
            return []

    def _find_page(self) -> Optional[Dict[str, Any]]:
        """找站内页面（用于页面内 fetch）。

        优先取普通标签页；若窗口当前停在别处（如本地面板），退而取任意标签页
        —— 因为只要该标签页所在浏览器已通过站点验证，同源 fetch 依然可用；
        真正的站点归属由 ensure_site_page() 兜底。
        """
        pages = self._list_pages()
        site = next((t for t in pages if "aifreeforever.com" in t.get("url", "")), None)
        if site:
            return site
        return pages[0] if pages else None

    def _new_tab(self, url: str) -> Optional[Dict[str, Any]]:
        """通过 CDP HTTP 接口新开标签页并返回其 target 信息。"""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/json/new?{urllib.parse.quote(url, safe='')}",
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            logger.warning("CDP 新开标签页失败: %s", e)
            return None

    def ensure_site_page(self, timeout: float = 45.0) -> bool:
        """确保窗口内有可用的站内页面；若被导航走则自动补开一个标签页。

        场景：用户（或工具）把窗口导航到了本地面板/其他站点，导致站内 fetch 失去
        上下文。此方法优先看是否已有站点标签页，否则新开一个并等待加载完成。
        """
        if any("aifreeforever.com" in t.get("url", "") for t in self._list_pages()):
            return True
        logger.info("窗口内无站内页面，自动新开标签页：%s", self.chat_page)
        tgt = self._new_tab(self.chat_page)
        if not tgt:
            return False
        wid = tgt.get("id")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(2)
            for t in self._list_pages():
                if t.get("id") == wid or "aifreeforever.com" in t.get("url", ""):
                    return True
        return any("aifreeforever.com" in t.get("url", "") for t in self._list_pages())

    @staticmethod
    def _is_site(page: Optional[Dict[str, Any]]) -> bool:
        return bool(page and "aifreeforever.com" in page.get("url", ""))

    def _active_page(self) -> Optional[Dict[str, Any]]:
        """返回当前窗口的前台标签页（Edge 在 /json 里不带该标记，退化用第一个非站内页）。"""
        for t in self._list_pages():
            if not self._is_site(t):
                return t
        return None

    def _activate(self, page: Dict[str, Any]) -> None:
        """把目标标签页提到前台。

        必须：Chrome/Edge 会对**后台标签页**的 JS 执行与网络做节流，导致
        Runtime.evaluate 长时间挂起（实测表现为 "CDP fetch 失败: Connection timed out"）。
        """
        tid = page.get("id")
        if not tid:
            return
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/json/activate/{tid}", method="GET"
            )
            with urllib.request.urlopen(req, timeout=8) as r:
                r.read()
        except Exception as e:  # noqa: BLE001
            logger.debug("激活标签页失败（忽略）: %s", e)

    async def _eval(self, expr: str, timeout: float = 300.0, restore_focus: bool = True) -> Any:
        import websocket

        page = self._find_page()
        if page and "aifreeforever.com" not in page.get("url", "") and "aifreeforever" in expr:
            # 当前标签页不在站点源上：页面内 fetch('/api/...') 会因跨源失败 →
            # 先补开一个站内标签页再执行
            logger.info("当前标签页非站内源（%s），自动补开站内标签页", page.get("url", "")[:60])
            self.ensure_site_page()
            page = self._find_page()
        if not page:
            raise RuntimeError("未找到可用的浏览器标签页（请确认 Edge 窗口已打开）")

        # 后台标签页会被浏览器节流（JS/网络挂起 → Connection timed out）→ 执行前提到前台，
        # 执行完把用户原本的前台标签还原，把视觉干扰降到最低。
        prev = self._active_page() if restore_focus else None
        switched = False
        if self._is_site(page) and (prev is None or prev.get("id") != page.get("id")):
            self._activate(page)
            switched = True
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=timeout)
        try:
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
                    }
                )
            )
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {}).get("result", {}).get("value")
        finally:
            ws.close()
            if switched and prev is not None:
                self._activate(prev)

    async def ensure_ready(self, timeout: float = 600.0) -> bool:
        """探测窗口会话是否可用。

        注意：本方法**不触发也不需要**人机验证。站点多数情况下不弹验证
        （cookie 有效时直接可用），因此这里的判定完全是「用一次看看能不能通」：

        - 页面正常 + API 返回 200  → 就绪（无论是否弹过验证）
        - 页面是 Cloudflare 挑战页 → 未就绪（此时才需要用户手动过一次）
        - 其他异常（超时/5xx/网络）→ 未就绪，但**不代表需要验证**

        窗口失联时自动重新拉起。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_alive():
                logger.warning("CDP 窗口失联，重新拉起 Edge...")
                self.launch()
                await asyncio.sleep(4)
            if not self.ensure_site_page(timeout=20.0):
                await asyncio.sleep(4)
                continue
            page = self._find_page()
            if page:
                try:
                    # 直接以「API 是否可达」为准，不依赖页面文本判断
                    probe = await self._eval(
                        """(async () => {
                            try {
                                const r = await fetch('https://aifreeforever.com/api/chat-models');
                                return JSON.stringify({status: r.status});
                            } catch (e) { return JSON.stringify({status: -1, err: String(e)}); }
                        })()""",
                        timeout=30,
                    )
                    status = _probe_status(probe)
                    if status == 200:
                        if not self._ready:
                            logger.info("会话可用（API 200）——无需人机验证")
                        self._ready = True
                        return True
                    if status in (403, 503):
                        # 只有这种情况才可能真的需要人工过验证；也仍可能是 IP 限流
                        challenge = self._page_is_challenge()
                        if challenge:
                            logger.info("需要人机验证：页面仍是 Cloudflare 挑战页，请在浏览器窗口内完成一次")
                        else:
                            logger.info("API 返回 %s，但页面未见验证挑战（可能是 IP 限流或上游异常）", status)
                    else:
                        logger.warning("[会话探测] API 返回非预期状态: %s", str(probe)[:100])
                except Exception as e:  # noqa: BLE001
                    logger.warning("[会话探测] 异常: %s", e)
            await asyncio.sleep(3)
        logger.warning("会话探测超时（%.0fs）：未取得 API 200 响应", timeout)
        return False

    def _page_is_challenge(self) -> bool:
        """页面当前是否停留在 Cloudflare 挑战页（同步 websocket 调用，失败即视为否）。"""
        try:
            import websocket

            page = self._find_page()
            if not page:
                return False
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=8)
            try:
                ws.send(json.dumps({
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "document.title + '||' + (document.body?document.body.innerText.slice(0,150):'')",
                        "returnByValue": True,
                    },
                }))
                while True:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == 1:
                        val = msg.get("result", {}).get("result", {}).get("value") or ""
                        title, _, body = val.partition("||")
                        return looks_like_challenge(body, title)
            finally:
                ws.close()
        except Exception:  # noqa: BLE001
            return False
        return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def live_probe(self) -> bool:
        """实时探测窗口会话是否可用（fetch /api/chat-models 是否 200），不依赖缓存标志。

        只有明确被拦截（403/503）或网络异常时才置为未就绪；
        其他状态码不轻易否定（避免把上游抖动误判成"需要验证"）。
        """
        try:
            res = await self.fetch("https://aifreeforever.com/api/chat-models", "GET", timeout=30.0)
            status = res.get("status")
            if status == 200:
                self._ready = True
                return True
            if status in (403, 503):
                self._ready = False
                return False
            # 其他状态（5xx 等）：保留原就绪标志，不因上游抖动而否定会话
            logger.warning("live_probe 返回 %s，保留原就绪状态 %s", status, self._ready)
            return self._ready
        except Exception as e:  # noqa: BLE001
            logger.warning("live_probe 异常（保留原就绪状态 %s）: %s", self._ready, e)
            return self._ready

    # ---------- 站内请求（页面内 fetch）----------

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        expr = f"""(async () => {{
            const r = await fetch('{url}', {{
                method: '{method}',
                headers: {json.dumps(headers or {})},
                body: {("JSON.stringify(" + json.dumps(body) + ")") if body is not None else 'undefined'}
            }});
            const t = await r.text();
            return JSON.stringify({{status: r.status, text: t.slice(0, 200000)}});
        }})()"""
        try:
            out = await self._eval(expr, timeout=timeout)
            parsed = json.loads(out or "{}")
            return {"ok": parsed.get("status") == 200, "status": parsed.get("status"), "text": parsed.get("text", "")}
        except Exception as e:  # noqa: BLE001
            logger.warning("CDP fetch 失败: %s", e)
            raise RuntimeError(f"CDP fetch 失败: {e}")

    async def fetch_multipart(
        self,
        url: str,
        field_name: str,
        filename: str,
        b64_content: str,
        content_type: str = "application/octet-stream",
        timeout: float = 120.0,
    ) -> Dict[str, Any]:
        """页面内 multipart 上传（FormData），供合规检查等接口使用。"""
        expr = f"""(async () => {{
            try {{
                const bin = atob({json.dumps(b64_content)});
                const arr = new Uint8Array(bin.length);
                for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                const blob = new Blob([arr], {{type: {json.dumps(content_type)}}});
                const fd = new FormData();
                fd.append({json.dumps(field_name)}, blob, {json.dumps(filename)});
                const r = await fetch('{url}', {{method: 'POST', body: fd}});
                const t = await r.text();
                return JSON.stringify({{status: r.status, text: t.slice(0, 200000)}});
            }} catch (e) {{
                return JSON.stringify({{status: -1, text: 'ERR: ' + (e && e.message || e)}});
            }}
        }})()"""
        try:
            out = await self._eval(expr, timeout=timeout)
            parsed = json.loads(out or "{}")
            return {"ok": parsed.get("status") == 200, "status": parsed.get("status"), "text": parsed.get("text", "")}
        except Exception as e:  # noqa: BLE001
            logger.warning("CDP fetch_multipart 失败: %s", e)
            return {"ok": False, "status": -1, "text": f"ERR: {e}"}

    async def stream_chat(
        self,
        url: str,
        payload: Dict[str, Any],
        on_data: Any,
        on_end: Any,
        timeout: float = 300.0,
    ) -> None:
        """页面内流式 fetch：一次性取回 SSE 全文后逐行回调（网关侧再转发为真流式）。"""
        expr = f"""(async () => {{
            const r = await fetch('{url}', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({json.dumps(payload)})
            }});
            const t = await r.text();
            return JSON.stringify({{status: r.status, ctype: r.headers.get('content-type'), text: t.slice(0, 400000)}});
        }})()"""
        out = await self._eval(expr, timeout=timeout)
        parsed = json.loads(out or "{}")
        logger.info("[stream_chat] status=%s ctype=%s text_len=%d head=%r",
                    parsed.get("status"), parsed.get("ctype"),
                    len(parsed.get("text", "")), parsed.get("text", "")[:80])
        if parsed.get("status") != 200:
            raise RuntimeError(f"上游 HTTP {parsed.get('status')}: {parsed.get('text','')[:200]}")
        text = parsed.get("text", "")
        saw_sse = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    saw_sse = True
                    await on_data(data)
        if not saw_sse:
            # 上游可能对短响应直接返回 JSON（{"answer": ...}，前端 Te() 同款兼容）→ 合成 token 事件
            try:
                body = json.loads(text)
            except Exception:  # noqa: BLE001
                body = {}
            answer = body.get("answer") or ""
            if answer:
                await on_data(json.dumps({"token": answer}, ensure_ascii=False))
            elif body.get("softBlock"):
                await on_data(json.dumps({"type": "error", "message": "上游容量已满（softBlock），请稍后重试"}))
            elif text.strip():
                # 兜底：非 JSON 非 SSE 原文当 token
                await on_data(json.dumps({"token": text.strip()[:4000]}, ensure_ascii=False))
        await on_end()

    def cookies(self) -> List[Dict[str, Any]]:
        try:
            import websocket

            page = self._find_page()
            if not page:
                return []
            ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
            ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            ws.recv()
            ws.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 2:
                    return msg.get("result", {}).get("cookies", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("CDP 读取 cookie 失败: %s", e)
            return []

    async def harvest_turnstile(self, timeout: float = 90.0) -> Optional[str]:
        """在窗口内采集 Cloudflare Turnstile token（改图/图生图通道需要）。

        实测结论（关键）：
        - Turnstile 必须用 **managed/normal 模式**渲染（invisible 模式不签发 token）。
        - 点击必须用 **CDP Input.dispatchMouseEvent**（受信任事件）；JS 的 el.click()
          是合成事件（isTrusted=false），Turnstile 拒绝签发。
        - 因此流程：注入脚本渲染可见 widget → 定位复选框 → 真实鼠标移动+点击 → 等 token。

        返回 token 字符串；失败返回 None。
        """
        sitekey = "0x4AAAAAADGj2nznqyRfB0Lj"
        try:
            from . import config as _cfg

            sitekey = getattr(_cfg.settings, "TURNSTILE_SITEKEY", sitekey) or sitekey
        except Exception:  # noqa: BLE001
            pass

        page = self._find_page()
        if not page:
            logger.warning("Turnstile 采集：未找到页面")
            return None

        import websocket

        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=timeout)
        _mid = [0]

        def call(method: str, params: Optional[Dict[str, Any]] = None, tmo: float = 60.0) -> Dict[str, Any]:
            _mid[0] += 1
            mid = _mid[0]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + tmo
            while time.time() < deadline:
                ws.settimeout(max(0.5, deadline - time.time()))
                try:
                    msg = json.loads(ws.recv())
                except Exception:  # noqa: BLE001
                    continue
                if msg.get("id") == mid:
                    res = msg.get("result", {})
                    if res.get("exceptionDetails"):
                        logger.warning("Turnstile CDP 异常: %s", str(res["exceptionDetails"])[:200])
                    return res
            return {}

        def ev(expr: str, tmo: float = 60.0) -> Any:
            r = call(
                "Runtime.evaluate",
                {"expression": expr, "returnByValue": True, "awaitPromise": True},
                tmo,
            )
            return r.get("result", {}).get("value")

        try:
            # 1) 注入并渲染 managed 模式 widget（可见）
            out = ev(
                f"""(async () => {{
                    try {{
                        if (!window.turnstile) {{
                            await new Promise((res, rej) => {{
                                const s = document.createElement('script');
                                s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
                                s.async = true; s.defer = true;
                                s.onload = () => res('ok');
                                s.onerror = () => rej(new Error('script load failed'));
                                document.head.appendChild(s);
                                setTimeout(() => rej(new Error('script load timeout')), 25000);
                            }});
                            await new Promise(r => setTimeout(r, 1500));
                        }}
                        let box = document.getElementById('__gw_ts_host');
                        if (box) box.remove();
                        box = document.createElement('div');
                        box.id = '__gw_ts_host';
                        box.style.cssText = 'position:fixed;left:50%;top:180px;transform:translateX(-50%);' +
                                            'z-index:2147483647;background:#fff;padding:14px;border:2px solid #888;';
                        document.body.appendChild(box);
                        window.__gwTsToken = null;
                        window.__gwTsErr = null;
                        if (window.__gwTsWidget !== undefined && window.turnstile.remove) {{
                            try {{ window.turnstile.remove(window.__gwTsWidget); }} catch (e) {{}}
                        }}
                        window.__gwTsWidget = window.turnstile.render('#__gw_ts_host', {{
                            sitekey: '{sitekey}',
                            size: 'normal',
                            theme: 'light',
                            callback: (t) => {{ window.__gwTsToken = t; }},
                            'error-callback': (e) => {{ window.__gwTsErr = String(e); }},
                        }});
                        return 'rendered';
                    }} catch (e) {{ return 'ERR: ' + (e && e.message || e); }}
                }})()""",
                tmo=90.0,
            )
            logger.info("Turnstile widget 渲染: %s", out)

            # 2) 等 host 出现并定位
            host = None
            for _ in range(12):
                await asyncio.sleep(1.0)
                raw = ev(
                    """(() => { const h = document.getElementById('__gw_ts_host'); if (!h) return null;
                        const r = h.getBoundingClientRect();
                        return JSON.stringify({x: r.x, y: r.y, w: r.width, h: r.height}); })()"""
                )
                if raw:
                    try:
                        host = json.loads(raw)
                        if host.get("w", 0) > 50 and host.get("h", 0) > 20:
                            break
                    except Exception:  # noqa: BLE001
                        host = None
            if not host:
                logger.warning("Turnstile 采集：widget 未渲染出可见尺寸")
                return None
            logger.info("Turnstile host 位置: %s", host)

            # 3) 真实鼠标移动 + 点击复选框（多次重试：实测第 1 次点击常只激活 widget，
            #    第 2 次才真正触发验证签发 token）
            tx = host["x"] + 30.0
            ty = host["y"] + host["h"] / 2.0
            x0, y0 = host["x"] - 120.0, host["y"] - 60.0
            deadline = time.time() + max(25.0, timeout - 25.0)
            attempt = 0
            while time.time() < deadline:
                attempt += 1
                for step in range(1, 7):
                    call(
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseMoved",
                            "x": x0 + (tx - x0) * step / 6.0,
                            "y": y0 + (ty - y0) * step / 6.0,
                            "buttons": 0,
                        },
                    )
                    await asyncio.sleep(0.07)
                await asyncio.sleep(0.35)
                call(
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": tx, "y": ty, "button": "left", "clickCount": 1, "buttons": 1},
                )
                call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": tx, "y": ty, "button": "left", "clickCount": 1, "buttons": 0},
                )
                logger.info("Turnstile 真实点击 #%d 已发送 (%d, %d)", attempt, int(tx), int(ty))

                # 每轮点击后等 token（约 8s）
                for _ in range(6):
                    await asyncio.sleep(1.4)
                    tok = ev("window.__gwTsToken || null")
                    if tok:
                        logger.info("Turnstile token 已获取（CDP 真实交互，第 %d 次点击），长度 %d", attempt, len(tok))
                        ev("(() => { const b = document.getElementById('__gw_ts_host'); if (b) b.remove(); return 1; })()")
                        return tok
                    err = ev("window.__gwTsErr || null")
                    if err:
                        logger.warning("Turnstile widget 报错: %s", err)
            logger.warning("Turnstile token 采集超时（%ss，共点击 %d 次）", timeout, attempt)
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("Turnstile CDP 采集异常: %s", e)
            return None
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        logger.info("CDP 桥已关闭")