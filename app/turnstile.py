"""Turnstile token 采集器：浏览器内触发站点验证码控件抓取 / 外部求解服务（可插拔）。

三种来源（按优先级）：
1. 外部求解服务（2captcha / CapMonster 等，API 形状一致的 AntiTurnstileTaskProxyLess 类任务）：
   - .env 配 TURNSTILE_SOLVER_URL / TURNSTILE_SOLVER_KEY 后启用。
   - 流程：createTask(type=AntiTurnstileTaskProxyLess, websiteURL, websiteKey) → 轮询 getTaskResult 拿 token。
   - 优点：完全不依赖本机浏览器反检测；缺点：token 由服务商 IP 签发，若被上游校验 IP 则可能失效（实测为准）。
2. 浏览器内采集（本机过验证的页面自动点复选框）——默认路径。
3. 无 token（仅适用于 /api/generate-image 聊天通道）。

站点 sitekey（逆向自前端 __PUBLIC_ENV__）：0x4AAAAAADGj2nznqyRfB0Lj
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

from . import config
from .session import BrowserManager

logger = logging.getLogger(__name__)

TOKEN_TTL = 110  # 秒（Turnstile token 约 2 分钟有效，留余量）
_TOKEN_CACHE: Dict[str, Any] = {}  # slot_key -> {"token": str, "ts": float}


def _cache_key(browser: Any) -> str:
    picker = getattr(browser, "_pick_slot", None)
    slot = picker() if callable(picker) else None
    return id(slot) if slot else f"cdp:{getattr(browser, 'port', 'na')}"


def get_cached_only(browser: Any) -> Optional[str]:
    """只读缓存（不触发采集）：TTL 内的 token 直接复用，用于请求前预填。"""
    key = _cache_key(browser)
    cached = _TOKEN_CACHE.get(key)
    if cached and (time.time() - cached["ts"]) < TOKEN_TTL:
        return cached["token"]
    return None


async def get_cached_or_harvest(browser: BrowserManager) -> Optional[str]:
    """优先取缓存 token（TTL 内），否则按配置走外部求解或浏览器内采集。"""
    key = _cache_key(browser)
    cached = _TOKEN_CACHE.get(key)
    if cached and (time.time() - cached["ts"]) < TOKEN_TTL:
        return cached["token"]
    token = await _solve_via_service() or await harvest_turnstile_token(browser)
    if token:
        _TOKEN_CACHE[key] = {"token": token, "ts": time.time()}
    return token


# ---------------------------------------------------------------------------
# 1. 外部求解服务（可选）
# ---------------------------------------------------------------------------

def _solver_enabled() -> bool:
    return bool(config.settings.TURNSTILE_SOLVER_URL and config.settings.TURNSTILE_SOLVER_KEY)


async def _solve_via_service(timeout: float = 120.0) -> Optional[str]:
    if not _solver_enabled():
        return None
    try:
        url = config.settings.TURNSTILE_SOLVER_URL.rstrip("/")
        site = config.settings.MAIN_ORIGIN + "/image-generators"
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            # 创建任务（2captcha / CapMonster 兼容形状）
            r = await c.post(
                f"{url}/createTask",
                json={
                    "clientKey": config.settings.TURNSTILE_SOLVER_KEY,
                    "task": {
                        "type": "AntiTurnstileTaskProxyLess",
                        "websiteURL": site,
                        "websiteKey": config.settings.TURNSTILE_SITEKEY,
                    },
                },
            )
            task_id = r.json().get("taskId")
            if not task_id:
                logger.warning("Turnstile solver createTask 失败: %s", r.text[:200])
                return None
            # 轮询
            deadline = time.time() + timeout
            while time.time() < deadline:
                await _sleep(4)
                rr = await c.post(
                    f"{url}/getTaskResult",
                    json={"clientKey": config.settings.TURNSTILE_SOLVER_KEY, "taskId": task_id},
                )
                data = rr.json()
                if data.get("status") == "ready":
                    token = data.get("solution", {}).get("token")
                    if token:
                        logger.info("Turnstile token（外包）已获取: %s", token[:24])
                        return token
                    break
                if data.get("errorId"):
                    logger.warning("Turnstile solver 错误: %s", data.get("errorDescription"))
                    break
            logger.warning("Turnstile solver 轮询超时")
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning("Turnstile solver 调用异常: %s", e)
        return None


# ---------------------------------------------------------------------------
# 2. 浏览器内采集（默认）
# ---------------------------------------------------------------------------

async def harvest_turnstile_token(browser: Any, timeout: float = 90.0) -> Optional[str]:
    """采集 Turnstile token：CDP 桥（真实鼠标事件）优先，Playwright 槽位兜底。

    为什么必须用真实交互事件：
    - Turnstile 的 invisible 模式不签发 token；必须 managed/normal 模式渲染出可见 widget。
    - JS 的 el.click() 是合成事件（isTrusted=false）→ Turnstile 拒绝签发。
    - CDP 的 Input.dispatchMouseEvent 生成受信任事件（等价真人点击）→ 正常签发（实测 5s 内拿到）。
    """
    # CDP 桥后端：走专用实现（真实输入事件）
    harvest = getattr(browser, "harvest_turnstile", None)
    if callable(harvest):
        return await harvest(timeout=timeout)
    return await _harvest_via_playwright(browser, timeout=timeout)


async def _harvest_via_playwright(browser: BrowserManager, timeout: float = 60.0) -> Optional[str]:
    """Playwright 槽位兜底采集（旧路径；注意：合成点击在真实站点大概率不签发 token）。"""
    slot = browser._pick_slot()
    if slot is None:
        await browser.ensure_ready(timeout=30.0)
        slot = browser._pick_slot()
    if slot is None:
        return None

    page = slot.page
    try:
        import asyncio

        async with asyncio.wait_for(slot.lock, timeout=min(timeout, 30.0)):
            # 新开 tab 避免打断正在使用的页面
            tab = await slot.context.new_page()
            tab.set_default_timeout(60000)
            try:
                await tab.goto(
                    f"{config.settings.MAIN_ORIGIN}/image-generators",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                deadline = time.time() + timeout
                token = None
                while time.time() < deadline:
                    await _sleep(3)
                    # 点击复选框
                    for f in tab.frames:
                        if "challenges.cloudflare.com" in f.url:
                            try:
                                box = f.locator("input[type=checkbox], .ctp-checkbox-label, .cb-lb")
                                if await box.count() > 0:
                                    await box.first.click(timeout=2000)
                            except Exception:  # noqa: BLE001
                                pass
                    # 从 DOM 读取 token
                    token = await tab.evaluate(
                        """() => {
                            const el = document.querySelector(
                                '[name="cf-turnstile-response"], [name="turnstileToken"]'
                            );
                            if (el && el.value) return el.value;
                            return window.__cfTurnstileToken || null;
                        }"""
                    )
                    if token:
                        logger.info("Turnstile token 已获取（浏览器，前 24 字符）: %s", token[:24])
                        return token
                logger.warning("Turnstile token 采集超时（%ss）", timeout)
                return None
            finally:
                try:
                    await tab.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception as e:  # noqa: BLE001
        logger.warning("Turnstile token 采集异常: %s", e)
        return None


async def _sleep(s: float) -> None:
    import asyncio

    await asyncio.sleep(s)