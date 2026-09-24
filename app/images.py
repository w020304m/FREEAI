"""图像生成 / 图像编辑（OpenAI Images API 兼容）。

上游（逆向确认）：
- 聊天内置文生图  POST /api/generate-image
    body: {prompt, resolution, output_format, seed, model_type:"fast", source:"chatbot", interactionProof}
    → {success, images:[url], imageUrl, chatBypassRemaining}（无需 turnstile token，但每 IP 每日限额 → 429）
- 图像工作台      POST /api/v2/generate-image
    body: {modelId, prompt, aspect_ratio, turnstileToken,
           referenceImageUrl, referenceImageUrls?}
    → {success, images:[url]}（严格需要 turnstile token）
    图生图官方流程（逆向自 ImageGeneratorWorkspaceApp）：
      1. POST /api/moderate-image  multipart file
      2. POST /api/upload-photo    multipart file → {url|imageUrl}
      3. POST /api/v2/generate-image，referenceImageUrl(s) 必须是步骤 2 的 **http(s) URL**
         （塞 data-URI / 裸 base64 会被上游拒绝）
- 合规检查        POST /api/moderate-image（multipart file → {result: pass}）

传输：一律在"已通过人机验证"的浏览器页面上下文内 fetch（可靠绕过 Cloudflare）。
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import config
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
            # 官方工作台：先 upload-photo 拿 http(s) URL，再作为 referenceImageUrl(s)。
            # 直接塞 data-URI / 裸 base64 会被上游拒绝（403/500）。
            uploaded: List[str] = []
            for im in reference_images[:3]:
                url = await self.upload_photo(im)
                if url:
                    uploaded.append(url)
            if not uploaded:
                return {"ok": False, "status": 502, "error": "参考图上传失败（/api/upload-photo 未返回 URL）"}
            body["referenceImageUrl"] = uploaded[0]
            if len(uploaded) > 1:
                body["referenceImageUrls"] = uploaded
        headers = {"x-api-secret": ""}
        # 工作台必须带 token。先读缓存，没有就采；不要先发空 token（会浪费一次，后面还容易 duplicate）。
        if not turnstile_token:
            try:
                from .turnstile import get_cached_only, get_cached_or_harvest

                turnstile_token = get_cached_only(self.session) or ""
                if not turnstile_token:
                    turnstile_token = await get_cached_or_harvest(self.session) or ""
                if turnstile_token:
                    body["turnstileToken"] = turnstile_token
            except Exception as e:  # noqa: BLE001
                logger.debug("读取/采集 token 失败: %s", e)
        if turnstile_token:
            headers["x-captcha-verified-at"] = str(int(time.time() * 1000))
            headers["x-turnstile-token"] = turnstile_token
        try:
            res = await self.session.fetch(
                config.settings.image_gen_v2_url, method="POST", body=body, headers=headers, timeout=240.0
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": -1, "error": f"浏览器 fetch 失败: {e}"}
        finally:
            # token 发出即作废（即使这次 503/403，也不能再拿去换模型）
            if turnstile_token or body.get("turnstileToken"):
                try:
                    from .turnstile import invalidate_cached

                    invalidate_cached(self.session)
                except Exception:  # noqa: BLE001
                    pass
        # 403 且提示缺 token → 尝试自动采集 Turnstile token 后重试一次
        if res.get("status") == 403 and (
            "missing-input-response" in res.get("text", "")
            or "timeout-or-duplicate" in res.get("text", "")
        ):
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
            text = res.get("text", "") or ""
            if reference_images and "at capacity" in text.lower():
                alt = await self._fallback_edit_model(model)
                if alt:
                    logger.warning("模型 %s 容量满，按官方 silent-fallback 改试 %s", model, alt)
                    body["modelId"] = alt
                    try:
                        from .turnstile import get_cached_or_harvest

                        fresh = await get_cached_or_harvest(self.session)
                        if fresh:
                            body["turnstileToken"] = fresh
                            headers = {
                                "x-api-secret": "",
                                "x-captcha-verified-at": str(int(time.time() * 1000)),
                                "x-turnstile-token": fresh,
                            }
                    except Exception as e:  # noqa: BLE001
                        logger.warning("换模型前重新采集 token 失败: %s", e)
                    try:
                        res = await self.session.fetch(
                            config.settings.image_gen_v2_url,
                            method="POST",
                            body=body,
                            headers=headers,
                            timeout=240.0,
                        )
                    except Exception as e2:  # noqa: BLE001
                        return {"ok": False, "status": -1, "error": f"换模型重试失败: {e2}"}
                    if not res.get("ok"):
                        return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
                    try:
                        data = json.loads(res.get("text", "{}"))
                    except Exception:  # noqa: BLE001
                        return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
                    urls = data.get("images") or []
                    return {
                        "ok": bool(data.get("success")),
                        "urls": urls,
                        "used_model": alt,
                        "head": res.get("text", "")[:400],
                    }
            return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
        try:
            data = json.loads(res.get("text", "{}"))
        except Exception:  # noqa: BLE001
            return {"ok": False, "status": res.get("status"), "head": res.get("text", "")[:300]}
        urls = data.get("images") or []
        return {"ok": bool(data.get("success")), "urls": urls, "head": res.get("text", "")[:400]}

    # 官方工作台 canRefImage/canEdit=true 的模型（逆向自 MODEL_REGISTRY）
    EDIT_MODELS = (
        "qwen-image",
        "seedream-4",
        "grok-imagine",
        "gpt-image-2-5-flare",
        "gpt-image-2-5-sunburst",
        "gpt-image-2",
    )

    async def _fallback_edit_model(self, current: str) -> str:
        """容量满时换一个仍支持参考图、且可用性更高的模型。"""
        avail: Dict[str, float] = {}
        try:
            res = await self.session.fetch(config.settings.model_availability_url, timeout=20.0)
            if res.get("ok"):
                data = json.loads(res.get("text") or "{}")
                for m in data.get("models") or []:
                    mid = m.get("modelId") or m.get("id") or ""
                    pct = m.get("availabilityPct")
                    if mid and pct is not None:
                        avail[mid] = float(pct)
        except Exception as e:  # noqa: BLE001
            logger.debug("读 model-availability 失败: %s", e)
        cands = [m for m in self.EDIT_MODELS if m != current]
        cands.sort(key=lambda m: avail.get(m, 0.0), reverse=True)
        for m in cands:
            if avail.get(m, 0.0) >= 20:
                return m
        return cands[0] if cands else ""

    # ---------- 参考图上传（图生图必须先拿到 http(s) URL）----------

    async def upload_photo(self, image_bytes: bytes, filename: str = "ref.png") -> str:
        """POST /api/upload-photo → 返回官方可引用的图片 URL。失败返回空串。"""
        try:
            b64 = base64.b64encode(image_bytes).decode()
            res = await self.session.fetch_multipart(
                config.settings.upload_photo_url, "file", filename, b64, "image/png"
            )
            if not res.get("ok"):
                logger.warning("upload-photo 失败 HTTP %s: %s", res.get("status"), (res.get("text") or "")[:200])
                return ""
            data = json.loads(res.get("text", "{}") or "{}")
            url = data.get("url") or data.get("imageUrl") or ""
            if not url:
                logger.warning("upload-photo 未返回 url: %s", (res.get("text") or "")[:200])
            return url
        except Exception as e:  # noqa: BLE001
            logger.warning("upload-photo 异常: %s", e)
            return ""

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
