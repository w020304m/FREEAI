"""aifreeforever-server 配置管理。"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "aifreeforever-server"
    APP_VERSION: str = "1.0.0"

    API_MASTER_KEY: Optional[str] = "1"
    # 鉴权模式：strict（默认，严格比对 API_MASTER_KEY，防止误连）| lenient（任意 key 均可）
    AUTH_MODE: str = "strict"

    PORT: int = 8110
    HOST: str = "0.0.0.0"

    # ---- 上游 ----
    MAIN_ORIGIN: str = "https://aifreeforever.com"
    # 聊天页（聊天接口 / 会话探测）
    CHAT_PAGE: str = "https://aifreeforever.com/chat/gpt-5-mini"
    # 图像工作台（图生图 / Turnstile 采集必须在这个页，chat 页经常签不出 token）
    IMAGE_PAGE: str = "https://aifreeforever.com/image-generators/gpt-image-2"

    # ---- 浏览器（会话获取 / 人机验证）----
    BROWSER_EXECUTABLE: str = ""
    BROWSER_USER_DATA_DIR: str = str(BASE_DIR / "browser_data")
    BROWSER_CONTEXTS: int = 2
    BROWSER_HEADLESS: bool = False  # 站点 Cloudflare 挑战会标记无头浏览器，默认有头

    # ---- 默认值 ----
    DEFAULT_CHAT_MODEL: str = "gpt-5-mini"
    DEFAULT_IMAGE_MODEL: str = "gpt-image-2"
    DEFAULT_ASPECT_RATIO: str = "1:1"

    UPSTREAM_TIMEOUT: float = 240.0

    # nonce 缓存秒数：省掉每次对话前取 nonce 的一次上游往返。
    # 上游拒绝请求时网关会自动作废缓存重新获取；若实测 nonce 为一次性可设为 0 关闭。
    NONCE_CACHE_TTL: float = 30.0

    # 会话（cookie）刷新间隔（秒）：cf_clearance 通常数小时过期
    COOKIE_REFRESH_INTERVAL: int = 7200

    # 模型缓存刷新间隔（秒）
    MODELS_CACHE_TTL: int = 3600

    LOG_LEVEL: str = "INFO"

    # ---- 会话后端 ----
    # 0 = 关闭 CDP 桥（只用 Playwright 浏览器上下文）
    # >0 = 使用 CDP 桥（推荐）：附加到"用户手动过验证的干净 Edge 窗口"（tools/harvest_zero.py 拉起的窗口）
    #     该窗口零自动化注入，页面内 fetch 是唯一稳定通过 Cloudflare 的通道。
    CDP_PORT: int = 9230

    # ---- 上游 API 路径（逆向自前端 JS，站点改版时调整）----
    @property
    def models_api_url(self) -> str:
        return f"{self.MAIN_ORIGIN}/api/chat-models"

    @property
    def nonce_api_url(self) -> str:
        return f"{self.MAIN_ORIGIN}/api/chat-nonce"

    @property
    def image_gen_chatbot_url(self) -> str:
        """聊天内置文生图（无 token，但每 IP 每日限额）。"""
        return f"{self.MAIN_ORIGIN}/api/generate-image"

    @property
    def image_gen_v2_url(self) -> str:
        """图像工作台文生图/图生图（需要 turnstile token）。"""
        return f"{self.MAIN_ORIGIN}/api/v2/generate-image"

    @property
    def moderate_image_url(self) -> str:
        return f"{self.MAIN_ORIGIN}/api/moderate-image"

    @property
    def model_availability_url(self) -> str:
        return f"{self.MAIN_ORIGIN}/api/v2/model-availability"

    @property
    def upload_photo_url(self) -> str:
        """图生图参考图上传。官方工作台：moderate → upload-photo → 把返回 URL 塞进 v2。"""
        return f"{self.MAIN_ORIGIN}/api/upload-photo"

    # 站点 Turnstile sitekey（逆向自前端 __PUBLIC_ENV__）
    TURNSTILE_SITEKEY: str = "0x4AAAAAADGj2nznqyRfB0Lj"

    # 外部 Turnstile 求解服务（可选；2captcha / CapMonster 等 AntiTurnstileTaskProxyLess 兼容 API）
    # 留空则使用浏览器内采集
    TURNSTILE_SOLVER_URL: str = ""
    TURNSTILE_SOLVER_KEY: str = ""

    # 出口代理（可选；解决 Cloudflare 对本机 IP 的信誉标记 — 配住宅/轮换代理后浏览器请求走代理）
    # 格式：http://user:pass@host:port 或 socks5://host:port（Playwright 仅支持 http(s)）
    OUTBOUND_PROXY: str = ""

    # 直连 HTTP 加速通道（默认关闭：实测 cf_clearance 与浏览器 TLS 指纹强绑定，
    # curl_cffi/httpx 直连会被 403；主通道为浏览器页面内 fetch。开启可先试直连再回退。）
    USE_DIRECT_HTTP: bool = False

    # ---- 模型 → 端点映射（逆向自 ChatRedesignInterface.js，站点改版时调整）----
    MODEL_ENDPOINTS: Dict[str, str] = {
        "gpt-5": "/api/generate-ai-answer",
        "deepseek-v4-flash": "/api/generate-ai-answer-deepseek",
        "kimi-k2-6": "/api/generate-ai-answer-foundry",
        "gpt-5-mini": "/api/generate-ai-answer-foundry",
        "deepseek-v3-2": "/api/generate-ai-answer-foundry",
        "gpt-5-4": "/api/generate-ai-answer-orbio",
        "gemini-3-1": "/api/generate-ai-answer-orbio",
        "grok-4": "/api/generate-ai-answer-orbio",
        "qwen3-7": "/api/generate-ai-answer-orbio",
        "llama-3-1": "/api/generate-ai-answer-orbio",
        "phi-4": "/api/generate-ai-answer-orbio",
        "gpt-5-nano": "/api/generate-ai-answer-ci",
        "gpt-4-1": "/api/generate-ai-answer-ci",
        "gpt-oss-120b": "/api/generate-ai-answer-ci",
        "gemini-3": "/api/generate-ai-answer-ci",
        "claude-haiku-4-5": "/api/generate-ai-answer-ci",
        "glm-5-2": "/api/generate-ai-answer-ci",
        "qwen3-6": "/api/generate-ai-answer-ci",
        "claude-sonnet-4-6": "/api/generate-ai-answer-vyce",
        "gpt-astra": "/api/generate-ai-answer-vyce",
        "deepseek-v4-flash-orbio": "/api/generate-ai-answer-orbio",
        "deepseek-v4-flash-ci": "/api/generate-ai-answer-ci",
        "deepseek-v4-flash-vyce": "/api/generate-ai-answer-vyce",
    }

    @property
    def known_chat_models(self) -> List[str]:
        return list(self.MODEL_ENDPOINTS.keys())


settings = Settings()