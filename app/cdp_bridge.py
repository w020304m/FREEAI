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

try:
    from socket import timeout as SocketTimeout  # noqa: N812
except ImportError:  # pragma: no cover
    SocketTimeout = TimeoutError

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

    def __init__(
        self,
        port: int = 9230,
        chat_page: str = "https://aifreeforever.com/chat/gpt-5-mini",
        image_page: str = "https://aifreeforever.com/image-generators/gpt-image-2",
    ) -> None:
        self.port = port
        self.chat_page = chat_page
        self.image_page = image_page
        self._proc: Optional[subprocess.Popen] = None
        self._ready = False
        self._last_launch = 0.0
        self._last_verify_prompt = 0.0
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
        """若 CDP 端口确实不可用，则启动一个干净浏览器窗口（零自动化注入）。

        安全约束（重要）：
        - 端口被占用时**绝不**再拉起新进程（重复拉起会抢端口并弄死已有窗口，
          表现为之后永久 WinError 10061、所有请求 503）。
        - 拉起操作有冷却时间，避免探测抖动引发反复拉起。
        - 只有在本进程自己拉起过窗口时才做最小化，不干扰用户已有窗口。
        """
        if self.is_alive():
            logger.info("检测到 CDP 端口 %d 有响应，附加到已有窗口", self.port)
            return

        # 端口仍被占用但 CDP 不响应：说明已有浏览器实例正在启动/繁忙，
        # 此时再拉起只会抢端口。等待其就绪即可。
        if self._port_in_use():
            logger.info("端口 %d 已被占用但 CDP 暂未响应，等待其就绪（不再拉起新进程）", self.port)
            for _ in range(10):
                time.sleep(1.0)
                if self.is_alive():
                    logger.info("CDP 端口 %d 已就绪", self.port)
                    return
            logger.warning("端口 %d 占用中但 CDP 持续无响应；如需重启请手动关闭该浏览器窗口", self.port)
            return

        # 冷却：避免瞬时故障导致反复拉起
        now = time.time()
        if now - self._last_launch < 20.0:
            logger.info("距上次拉起仅 %.0fs，跳过本次（冷却中）", now - self._last_launch)
            return
        self._last_launch = now

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
        # 注意：不要用 --start-minimized 或屏幕外坐标 —— 它们会把窗口放到
        # (-32000,-32000) 屏幕外角落，用户点击任务栏恢复时"点不开"。
        # 正确做法：正常启动，端口就绪后用 ShowWindow(SW_MINIMIZE) 最小化，
        # 窗口保留在屏幕内，任务栏可点击恢复；需要人机验证时由
        # request_human_verification() 呼出到前台。
        # 出口代理（会话绑代理出口 IP：住宅代理可显著延长 cf_clearance 有效期 + 每 IP 图像配额按出口计）
        proxy = getattr(_cfg.settings, "OUTBOUND_PROXY", "") or ""
        if proxy:
            args.append(f"--proxy-server={proxy}")
            logger.info("窗口挂载出口代理: %s", proxy)
        args.append(self.chat_page)
        logger.info("启动浏览器窗口（零自动化注入，端口 %d）...", self.port)
        try:
            self._proc = subprocess.Popen(args)
        except FileNotFoundError:
            logger.error("找不到浏览器: %s", browser)
            return
        # 等待端口就绪后最小化，降低视觉干扰
        for _ in range(20):
            time.sleep(1.0)
            if self.is_alive():
                n = self._minimize_windows()
                if n:
                    logger.info("已最小化 %d 个浏览器窗口（会话保持后台运行）", n)
                return

    def is_alive(self) -> bool:
        """CDP 端口是否可用。

        注意：单次探测失败**不等于**窗口已死（浏览器忙、GC 停顿都可能瞬时失败），
        因此重试数次后才判定为不可用，避免误触发 launch() 把健康窗口弄坏。
        """
        for attempt in range(3):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=5) as r:
                    if r.status == 200:
                        return True
            except Exception:  # noqa: BLE001
                pass
            if attempt < 2:
                time.sleep(0.4)
        return False

    def _port_in_use(self) -> bool:
        """端口是否被占用（即使 CDP 不响应）。用于避免重复拉起进程。"""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def _window_cdp(self, action: str) -> int:
        """通过 CDP Browser domain 操作站点窗口（minimize / restore）。

        为什么不用 ctypes 按 PID：uvicorn 重启后本进程可能没有 launch 记录
        （_proc 为 None），而窗口仍在。CDP Browser.getWindowForTarget 直接
        以 target 定位窗口，与进程无关，最可靠。

        - minimize：把窗口摆回工作区（修复 Edge 记忆屏幕外位置导致的"点不开"）
          再用 minimized 状态最小化 —— 不抢焦点（用户当前窗口不受打扰）。
        - restore：窗口恢复并聚焦（用户需要过人机验证时呼出）。
        """
        if os.name != "nt":
            return 0
        try:
            import websocket

            ver = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=5).read()
            )
            ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=10)
            try:
                _id = [0]

                def call(method, params=None):
                    _id[0] += 1
                    mid = _id[0]
                    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                    while True:
                        msg = json.loads(ws.recv())
                        if msg.get("id") == mid:
                            return msg.get("result", {})

                n = 0
                for p in self._list_pages():
                    if not self._is_site(p):
                        continue
                    res = call("Browser.getWindowForTarget", {"targetId": p.get("id")})
                    wid = res.get("windowId")
                    if wid is None:
                        continue
                    if action == "minimize":
                        # 先恢复窗口为屏幕内位置（防"点不开"），再最小化不抢焦点
                        call("Browser.setWindowBounds", {
                            "windowId": wid,
                            "bounds": {"windowState": "normal", "left": 120, "top": 60,
                                       "width": 1180, "height": 780},
                        })
                        call("Browser.setWindowBounds", {
                            "windowId": wid, "bounds": {"windowState": "minimized"},
                        })
                    else:  # restore
                        call("Browser.setWindowBounds", {
                            "windowId": wid,
                            "bounds": {"windowState": "normal", "left": 120, "top": 60,
                                       "width": 1180, "height": 780},
                        })
                    n += 1
                return n
            finally:
                ws.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("CDP 窗口操作失败（忽略）: %s", e)
            return 0

    def _minimize_windows(self) -> int:
        """把站点窗口移回屏幕内并以"不抢焦点"方式最小化（不依赖本进程记录）。"""
        return self._window_cdp("minimize")

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

    def _find_page(self, prefer: str = "") -> Optional[Dict[str, Any]]:
        """找站内页面（用于页面内 fetch）。

        prefer:
          - "image"：优先图像工作台页（Turnstile / 图生图必须在这里采 token）
          - "chat"：优先聊天页
          - 空：任意站内页
        """
        pages = self._list_pages()
        site = [t for t in pages if "aifreeforever.com" in t.get("url", "")]
        if prefer == "image":
            hit = next((t for t in site if "/image-generators" in t.get("url", "")), None)
            if hit:
                return hit
        if prefer == "chat":
            hit = next((t for t in site if "/chat/" in t.get("url", "")), None)
            if hit:
                return hit
        if site:
            return site[0]
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

    def _wait_tab(self, url_part: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(url_part in t.get("url", "") for t in self._list_pages()):
                return True
            time.sleep(1.0)
        return any(url_part in t.get("url", "") for t in self._list_pages())

    def ensure_site_page(self, timeout: float = 45.0) -> bool:
        """确保同时有聊天页 + 图像工作台页。

        实测：同源 fetch 两边都能打 API；但 Turnstile 只在工作台页稳定签发。
        双 tab 常驻，按请求类型选页，避免改图时跑到 chat 页采 token。
        """
        pages = self._list_pages()
        has_chat = any("/chat/" in t.get("url", "") and "aifreeforever.com" in t.get("url", "") for t in pages)
        has_img = any("/image-generators" in t.get("url", "") for t in pages)
        if not has_chat and not any("aifreeforever.com" in t.get("url", "") for t in pages):
            logger.info("窗口内无站内页面，新开聊天页：%s", self.chat_page)
            if not self._new_tab(self.chat_page):
                return False
            if not self._wait_tab("aifreeforever.com", timeout):
                return False
            has_chat = True
        elif not has_chat:
            logger.info("补开聊天页：%s", self.chat_page)
            self._new_tab(self.chat_page)
            has_chat = self._wait_tab("/chat/", min(timeout, 20.0))
        if not has_img:
            logger.info("补开图像工作台页（改图/Turnstile）：%s", self.image_page)
            self._new_tab(self.image_page)
            has_img = self._wait_tab("/image-generators", min(timeout, 20.0))
        return has_chat or has_img or any("aifreeforever.com" in t.get("url", "") for t in self._list_pages())

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
        """把目标标签页提到前台（仅在人机验证等确需用户操作时调用）。

        注意：每请求调用会把最小化/后台的窗口弹到前台，干扰用户 ——
        因此常规请求**不**激活；只有超时重试或需要人工验证时才激活。
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

    def _restore_windows(self) -> int:
        """把站点窗口恢复并聚焦到前台（用户需人机验证时调用）。"""
        return self._window_cdp("restore")

    def request_human_verification(self, reason: str = "") -> None:
        """把浏览器窗口呼出到前台，提示用户完成一次人工验证。

        在检测到上游确实需要人机验证（challenge 页 / Turnstile 403）时调用，
        让"平时静默、必要时打扰"成为默认行为。
        """
        logger.info("需要人工验证%s——已将浏览器窗口呼出到前台，请在窗口内完成验证", f"（{reason}）" if reason else "")
        page = self._find_page()
        if page:
            self._activate(page)
        n = self._restore_windows()
        if n:
            logger.info("已恢复 %d 个浏览器窗口", n)

    async def _eval_once(
        self, page: Dict[str, Any], expr: str, timeout: float
    ) -> Any:
        """对指定页面执行一次 evaluate（不激活、不切换前台）。"""
        import websocket

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

    async def _eval(self, expr: str, timeout: float = 300.0, prefer: str = "") -> Any:
        page = self._find_page(prefer=prefer)
        if page and "aifreeforever.com" not in page.get("url", "") and "aifreeforever" in expr:
            # 当前标签页不在站点源上：页面内 fetch('/api/...') 会因跨源失败 →
            # 先补开一个站内标签页再执行
            logger.info("当前标签页非站内源（%s），自动补开站内标签页", page.get("url", "")[:60])
            self.ensure_site_page()
            page = self._find_page(prefer=prefer)
        if not page:
            raise RuntimeError("未找到可用的浏览器标签页（请确认浏览器窗口已打开）")

        # 常规路径：不激活、不切换前台，避免窗口自动弹出打扰用户。
        # 仅当后台标签执行超时（浏览器节流）时，才激活一次并重试（并在结束时还原前台标签）。
        try:
            return await self._eval_once(page, expr, timeout)
        except Exception as e:  # noqa: BLE001
            is_timeout = isinstance(e, (TimeoutError, SocketTimeout)) \
                or (hasattr(e, "__class__") and "Timeout" in e.__class__.__name__) \
                or "timed out" in str(e).lower()
            if not is_timeout:
                raise
            logger.info("后台标签执行超时，激活站点标签页后重试一次（不改变窗口显示状态）")
            prev = self._active_page()
            self._activate(page)
            try:
                return await self._eval_once(page, expr, timeout)
            finally:
                if prev is not None:
                    self._activate(prev)

    async def ensure_ready(self, timeout: float = 600.0) -> bool:
        """探测窗口会话是否可用。

        注意：本方法**不触发也不需要**人机验证。站点多数情况下不弹验证
        （cookie 有效时直接可用），因此这里的判定完全是「用一次看看能不能通」：

        - 页面正常 + API 返回 200  → 就绪（无论是否弹过验证）
        - 页面是 Cloudflare 挑战页 → 未就绪（此时才需要用户手动过一次），
          并**把浏览器窗口呼出到前台**（限频，避免反复打扰）让用户完成验证
        - 其他异常（超时/5xx/网络）→ 未就绪，但**不代表需要验证**

        窗口失联时自动重新拉起。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.is_alive():
                logger.warning("CDP 窗口失联，重新拉起浏览器...")
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
                            now = time.time()
                            if now - self._last_verify_prompt > 60:
                                self._last_verify_prompt = now
                                self.request_human_verification("检测到 Cloudflare 挑战页")
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
        prefer: str = "",
    ) -> Dict[str, Any]:
        if not prefer and ("generate-image" in url or "upload-photo" in url or "moderate-image" in url):
            prefer = "image"
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
            out = await self._eval(expr, timeout=timeout, prefer=prefer)
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
            out = await self._eval(expr, timeout=timeout, prefer="image")
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

        self.ensure_site_page(timeout=25.0)
        page = self._find_page(prefer="image")
        if not page:
            logger.warning("Turnstile 采集：未找到页面")
            return None
        logger.info("Turnstile 采集页: %s", (page.get("url") or "")[:80])

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
            # 最小化 / 后台 tab 里 Turnstile 不签发（document.hidden=true）。
            # 采集期间：恢复窗口 + 激活工作台 tab，等到可见再点。
            try:
                self._restore_windows()
                self._activate(page)
            except Exception:  # noqa: BLE001
                pass
            for _ in range(20):
                vis = ev("document.visibilityState")
                if vis == "visible":
                    break
                await asyncio.sleep(0.25)
            logger.info("Turnstile 采集前 visibility=%s", ev("document.visibilityState"))
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
            try:
                self._minimize_windows()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        logger.info("CDP 桥已关闭")