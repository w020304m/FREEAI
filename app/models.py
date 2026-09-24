"""模型注册表：合并上游聊天模型 + 图像模型，对外提供能力标注。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from . import config
from .session import BrowserManager

logger = logging.getLogger(__name__)

# 上游图像模型（逆向自 image-generators 页面链接 + Aggregate-to-2api）
IMAGE_MODELS: Dict[str, Dict[str, Any]] = {
    "gpt-image-2": {"display": "GPT Image 2", "cap": ["image", "image_edit"], "ratios": ["1:1", "3:2", "2:3", "16:9", "9:16"]},
    "gpt-image-2-5-flare": {"display": "GPT Image 2.5 Flare", "cap": ["image"], "ratios": None},
    "gpt-image-2-5-sunburst": {"display": "GPT Image 2.5 Sunburst", "cap": ["image"], "ratios": None},
    "gpt-image-1-5": {"display": "GPT Image 1.5", "cap": ["image"], "ratios": None},
    "flux-fast": {"display": "FLUX Fast", "cap": ["image"], "ratios": None},
    "flux-schnell": {"display": "FLUX Schnell", "cap": ["image"], "ratios": None},
    "z-image-turbo": {"display": "Z-Image Turbo", "cap": ["image"], "ratios": None},
    "p-image": {"display": "P-Image", "cap": ["image"], "ratios": None},
    "hidream-l1-fast": {"display": "HiDream L1 Fast", "cap": ["image"], "ratios": None},
    "nano-banana": {"display": "Nano Banana", "cap": ["image"], "ratios": None},
    "nano-banana-2": {"display": "Nano Banana 2", "cap": ["image"], "ratios": None},
    "nano-banana-pro": {"display": "Nano Banana Pro", "cap": ["image"], "ratios": None},
    "seedream-4": {"display": "Seedream 4", "cap": ["image", "image_edit"], "ratios": None},
    "seedream-4-5": {"display": "Seedream 4.5", "cap": ["image"], "ratios": None},
    "seedream-5": {"display": "Seedream 5 Lite", "cap": ["image"], "ratios": None},
    "qwen-image": {"display": "Qwen Image", "cap": ["image", "image_edit"], "ratios": None},
    "imagen-4": {"display": "Imagen 4 Fast", "cap": ["image"], "ratios": None},
    "imagen-3": {"display": "Imagen 3 Fast", "cap": ["image"], "ratios": None},
    "ideogram-v3": {"display": "Ideogram V3 Turbo", "cap": ["image"], "ratios": None},
    "grok-imagine": {"display": "Grok Imagine", "cap": ["image", "image_edit"], "ratios": None},
    "flux-2-pro": {"display": "FLUX 2 Pro", "cap": ["image"], "ratios": None},
    "flux-2-max": {"display": "FLUX 2 Max", "cap": ["image"], "ratios": None},
}


class ModelRegistry:
    def __init__(self, session: BrowserManager) -> None:
        self.session = session
        self._cache: Optional[Dict[str, Any]] = None
        self._last_updated: float = 0.0
        self._lock = asyncio.Lock()

    def _fallback_chat_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": mid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "aifreeforever",
                "capabilities": ["chat"],
                "endpoint": ep,
            }
            for mid, ep in config.settings.MODEL_ENDPOINTS.items()
        ]

    async def list_models(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and self._cache and (now - self._last_updated < config.settings.MODELS_CACHE_TTL):
            return self._cache

        async with self._lock:
            now = time.time()
            if not force and self._cache and (now - self._last_updated < config.settings.MODELS_CACHE_TTL):
                return self._cache

            chat_models = self._fallback_chat_models()
            try:
                res = await self.session.fetch(config.settings.models_api_url, method="GET", timeout=30.0)
                if res.get("ok"):
                    payload = json.loads(res.get("text", "{}"))
                    upstream = payload.get("models", [])
                    chat_models = []
                    for m in upstream:
                        mid = m.get("id")
                        chat_models.append(
                            {
                                "id": mid,
                                "object": "model",
                                "created": int(time.time()),
                                "owned_by": m.get("provider", "aifreeforever"),
                                "capabilities": ["chat"],
                                "available": bool(m.get("available")),
                                "name": m.get("name"),
                                "endpoint": config.settings.MODEL_ENDPOINTS.get(mid, ""),
                            }
                        )
                    logger.info("上游聊天模型刷新：%d 个", len(chat_models))
                else:
                    logger.warning("上游模型列表获取异常: HTTP %s", res.get("status"))
            except Exception as e:  # noqa: BLE001
                logger.error("获取上游模型列表失败，使用回退列表: %s", e)

            image_models = [
                {
                    "id": mid,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "aifreeforever",
                    "capabilities": meta["cap"],
                    "display_name": meta["display"],
                }
                for mid, meta in IMAGE_MODELS.items()
            ]

            data = {"object": "list", "data": chat_models + image_models}
            self._cache = data
            self._last_updated = time.time()
            return data

    def is_image_model(self, model: str) -> bool:
        return model in IMAGE_MODELS or model in ("gpt-image-2",)

    def image_model_info(self, model: str) -> Optional[Dict[str, Any]]:
        return IMAGE_MODELS.get(model)
