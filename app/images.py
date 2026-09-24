"""图像生成 / 图像编辑（OpenAI Images API 兼容）。

上游（逆向确认）：
- 聊天内置文生图  POST /api/generate-image
    body: {prompt, resolution, output_format, seed, model_type:"fast", source:"chatbot", interactionProof}
    → {success, images:[url], imageUrl, chatBypassRemaining}（无需 turnstile token，但每 IP 每日限额 → 429）
- 图像工作台      POST /api/v2/generate-image
    body: {modelId, prompt, aspect_ratio, turnstileToken, referenceImageUrl(s)?}
    → {success, images:[url]}（严格需要 turnstile token；图生图走此路）
- 合规检查        POST /api/moderate-image（multipart file → {result: pass}）

传输：一律在"已通过人机验证"的浏览器页面上下文内 fetch（可靠绕过 Cloudflare）。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import config, formats
from .session import BrowserManager

logger = logging.getLogger(__name__)

RESOLUTIONS = {
    "1:1": "1024 x 1024 (Square)",
    "3:2": "1536 x 1024 (Landscape)",
    "2:3": "1024 x 1536 (Portrait)",
    "3:4": "1024 x 1365 (Portrait)",
    "4:3": "1365 x 1024 (Landscape)",
    "16:9": "1792 x 1024 (Widescreen)",
    "9:16": "1024 x 1792 (Tall)",
}


class ImageProvider:
    def __init__(self, session: BrowserManager) -> None:
        self.session = session

    async def _nonce_proof(self, question: str = "") -> Dict[str, Any]:
        nonce = ""
        try:
            res = await self.session.fetch(config.settings.nonce_api_url, method="GET", timeout=30.0)
            if res.get("ok"):
                nonce = json.loads(res.get("text", "{}")).get("nonce", "")
        except Exception as e:  # noqa: BLE001
            logger.warning("获取 nonce 失败: %s", e)
        now_ms = int(time.time() * 1000)
        typing_ms = min(max(len(question) * 40, 300), 8000)
        return {
            "nonce": nonce,
            "keystrokeCount": min(max(len(question) // 10, 1), 30),
            "pasteEvents": 0,
            "totalTypingTime": typing_ms,
            "startTime": now_ms - typing_ms - 200,
            "submitTime": now_ms,
        }

    # ---------- 文生图（优先 chatbot 免 token 通道）----------

    async def generate_chatbot(
        self, prompt: str, aspect_ratio: str = "1:1", output_format: str = "png"
    ) -> Dict[str, Any]:
        proof = await self._nonce_proof(prompt)
        resolution = RESOLUTIONS.get(aspect_ratio, "1024 x 1024 (Square)")
        payload = {
            "prompt": prompt,
            "resolution": resolution,
            "output_format": output_format,
            "seed": -1,
            "model_type": "fast",
            "source": "chatbot",
            "interactionProof": proof,
        }
        try:
            res = await self.session.fetch(
                config.settings.image_gen_chatbot_url, method="POST", body=payload, timeout=180.0
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": -1, "error": f"浏览器 fetch 失败: {e}"}
        # 上游配额收紧时聊天通道会要求 captcha → 自动采 turnstile token 重试一次
        if res.get("status") == 403 and "requireCaptcha" in res.get("text", ""):
            logger.warning("聊天文生图通道要求 captcha，尝试自动采集 Turnstile token 并重试")
            try:
                from .turnstile import get_cached_or_harvest

                token = await get_cached_or_harvest(self.session)
                if token:
                    payload["turnstileToken"] = token
                    extra = {
                        "x-api-secret": "",
                        "x-captcha-verified-at": str(int(time.time() * 1000)),
                        "x-turnstile-token": token,
                    }
                    res = await self.session.fetch(
                        config.settings.image_gen_chatbot_url, method="POST",
                        body=payload, headers=extra, timeout=180.0,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning("Turnstile 自动采集失败: %s", e)
        if res.get("status") == 429:
            try:
                detail = json.loads(res.get("text", "{}"))
            except Exception:  # noqa: BLE001
                detail = {}
            return {
                "ok": False,
                "status": 429,
                "error": f"每 IP 每日限额（聊天通道），{detail.get('waitTime', '稍后')} 秒后重试",
            }
        if not res.get("ok"):
            return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
        try:
            data = json.loads(res.get("text", "{}"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
        urls = data.get("images") or ([data["imageUrl"]] if data.get("imageUrl") else [])
        return {"ok": True, "urls": urls, "remaining": data.get("chatBypassRemaining")}

    # ---------- 图像工作台（需 turnstile token；图生图）----------

    async def generate_v2(
        self,
        model: str,
        prompt: str,
        aspect_ratio: str = "1:1",
        reference_images: Optional[List[bytes]] = None,
        turnstile_token: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "modelId": model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "turnstileToken": turnstile_token,
        }
        if reference_images:
            refs = [formats.make_data_uri(im, "image/png") for im in reference_images[:3]]
            if len(refs) == 1:
                body["referenceImageUrl"] = refs[0]
            else:
                body["referenceImageUrls"] = refs
        headers = {"x-api-secret": ""}
        # 优先复用缓存 token（TTL 内），避免"先发空 token 撞 403 再采集"的两段式浪费
        if not turnstile_token:
            try:
                from .turnstile import get_cached_only

                turnstile_token = get_cached_only(self.session) or ""
                if turnstile_token:
                    body["turnstileToken"] = turnstile_token
            except Exception as e:  # noqa: BLE001
                logger.debug("读取缓存 token 失败: %s", e)
        if turnstile_token:
            headers["x-captcha-verified-at"] = str(int(time.time() * 1000))
            headers["x-turnstile-token"] = turnstile_token
        try:
            res = await self.session.fetch(
                config.settings.image_gen_v2_url, method="POST", body=body, headers=headers, timeout=240.0
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": -1, "error": f"浏览器 fetch 失败: {e}"}
        # 403 且提示缺 token → 尝试自动采集 Turnstile token 后重试一次
        if res.get("status") == 403 and "missing-input-response" in res.get("text", ""):
            logger.warning("v2 图像生成缺 Turnstile token，尝试自动采集并重试")
            try:
                from .turnstile import get_cached_or_harvest

                token = await get_cached_or_harvest(self.session)
                if token:
                    body["turnstileToken"] = token
                    headers = {
                        "x-api-secret": "",
                        "x-captcha-verified-at": str(int(time.time() * 1000)),
                        "x-turnstile-token": token,
                    }
                    try:
                        res = await self.session.fetch(
                            config.settings.image_gen_v2_url,
                            method="POST",
                            body=body,
                            headers=headers,
                            timeout=240.0,
                        )
                    except Exception as e2:  # noqa: BLE001
                        return {"ok": False, "status": -1, "error": f"重试 fetch 失败: {e2}"}
            except Exception as e:  # noqa: BLE001
                logger.warning("Turnstile 自动采集失败: %s", e)
        if res.get("status") == 429:
            try:
                wait = json.loads(res.get("text", "{}")).get("waitTime") or 60
            except Exception:  # noqa: BLE001
                wait = 60
            return {"ok": False, "status": 429, "error": f"每 IP 每日限额，约 {wait} 秒后重试"}
        if not res.get("ok"):
            return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
        try:
            data = json.loads(res.get("text", "{}"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
        urls = data.get("images") or []
        return {"ok": bool(data.get("success")), "urls": urls, "head": res.get("text", "")[:400]}

    # ---------- 合规检查 ----------

    async def moderate(self, image_bytes: bytes) -> bool:
        try:
            b64 = base64.b64encode(image_bytes).decode()
            res = await self.session.fetch_multipart(
                config.settings.moderate_image_url, "file", "edit.png", b64, "image/png"
            )
            if res.get("ok"):
                return json.loads(res.get("text", "{}")).get("result") == "pass"
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning("moderate 检查失败: %s", e)
            return True  # 失败不阻塞（上游部分场景不强制）

    # ---------- 下载图片 → b64 ----------

    async def to_b64(self, url: str) -> str:
        try:
            if not (url.startswith("https://") or url.startswith("http://")):
                logger.warning("拒绝下载非 http 协议的图片 URL: %s", url[:80])
                return ""
            import httpx

            async with httpx.AsyncClient(timeout=60.0, trust_env=False) as c:
                r = await c.get(url)
                r.raise_for_status()
                return base64.b64encode(r.content).decode()
        except Exception as e:  # noqa: BLE001
            logger.warning("图片下载失败 %s: %s", url, e)
            return ""
