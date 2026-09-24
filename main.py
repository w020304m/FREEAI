"""aifreeforever-server · FastAPI 入口。

把 aifreeforever.com 的站内 API 反代并转换成 OpenAI / Anthropic 兼容格式。

路由一览：
- POST /v1/chat/completions    OpenAI 聊天补全（流式/非流式）
- POST /v1/messages            Anthropic Messages（流式/非流式）
- GET  /v1/models              模型列表（聊天 + 图像，含能力标注）
- POST /v1/images/generations  文生图
- POST /v1/images/edits        图生图（图像修改）
- POST /v1/files               文件上传（本地代理存储）
- GET  /v1/files / {id} / {id}/content / DELETE /v1/files/{id}
- POST /v1/video               视频生成（上游未提供 → 501，说明见响应）
- POST /v1/fine_tuning/jobs    微调（上游未提供 → 501，说明见响应）
- GET  /health                 健康检查（含会话就绪状态）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app import config
from app.chat import ChatProvider
from app.files import file_store
from app.images import ImageProvider
from app.models import ModelRegistry
from app.session import BrowserManager
from app.cdp_bridge import CdpBridge

logging.basicConfig(
    level=getattr(logging, config.settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---- 全局单例 ----
session_manager: Any
chat_provider: ChatProvider
image_provider: ImageProvider
model_registry: ModelRegistry


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_manager, chat_provider, image_provider, model_registry

    logger.info("%s v%s 启动中 ...", config.settings.APP_NAME, config.settings.APP_VERSION)
    if config.settings.CDP_PORT > 0:
        # 推荐后端：CDP 桥（干净 Edge 窗口 + 用户手动过验证 + 页面内 fetch）
        session_manager = CdpBridge(
            port=config.settings.CDP_PORT,
            chat_page=config.settings.CHAT_PAGE,
        )
        session_manager.launch()
    else:
        # 备用后端：Playwright 浏览器上下文
        session_manager = BrowserManager(
            chat_page=config.settings.CHAT_PAGE,
            user_data_dir=config.settings.BROWSER_USER_DATA_DIR,
            contexts=config.settings.BROWSER_CONTEXTS,
            executable_path=config.settings.BROWSER_EXECUTABLE,
            headless=config.settings.BROWSER_HEADLESS,
        )
        await session_manager.initialize()

    chat_provider = ChatProvider(session_manager)
    image_provider = ImageProvider(session_manager)
    model_registry = ModelRegistry(session_manager)

    # 后台完成会话获取（不阻塞服务启动）
    asyncio.create_task(_background_ready())

    yield

    logger.info("关闭中 ...")
    await session_manager.shutdown()


async def _background_ready():
    """后台低频率探测会话可用性。

    设计要点：站点**通常不需要**人机验证（cookie 有效时直接可用）。
    因此这里只做低频探测，不主动触发任何验证流程；
    探测频率刻意放低（就绪后 60s、未就绪 10s），因为高频请求本身
    可能招致 Cloudflare 重新挑战。
    """
    try:
        while True:
            ok = await session_manager.ensure_ready(timeout=120.0)
            if ok:
                logger.info("会话可用")
                await asyncio.sleep(60)  # 已就绪：低频维持，避免自招挑战
                continue
            logger.info("会话暂不可用，10 秒后重试（若页面显示 Cloudflare 验证，请在浏览器窗口内手动完成一次）")
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("后台会话探测任务异常: %s", e)


# ---- 鉴权 ----

async def verify_api_key(request: Request, authorization: Optional[str] = Header(default=None)):
    """鉴权。默认 strict 严格比对 API_MASTER_KEY（防误连/被扫）。

    .env 配 AUTH_MODE=lenient 时任意 key 均可；API_MASTER_KEY 留空或 "1" 关闭鉴权。
    健康检查 /health 与根路径 / 豁免鉴权（便于探活）。
    """
    if request.url.path in ("/health", "/", "/panel") or request.url.path.startswith("/ui"):
        return
    key = config.settings.API_MASTER_KEY
    if not key or key == "1":
        return
    if config.settings.AUTH_MODE != "strict":
        return  # 宽松模式：工具要求填 key 时随意填写即可
    if not authorization or "bearer" not in authorization.lower():
        raise HTTPException(status_code=401, detail="需要 Bearer Token 认证。")
    token = authorization.split(" ")[-1].strip()
    import hmac

    if not hmac.compare_digest(token, key):
        raise HTTPException(status_code=403, detail="无效的 API Key。")


async def check_ready():
    # CDP 桥模式：只做轻量判断（窗口存活 + 就绪标志），不做每请求实时探测
    # （实测：高频 API 探测本身会加速触发 Cloudflare 重新挑战；
    #   真实探测由后台 _background_ready 低频循环负责维护 _ready 标志）
    if config.settings.CDP_PORT > 0:
        if not session_manager.is_alive():
            raise HTTPException(
                status_code=503,
                detail="浏览器窗口已关闭。请重新运行 start.bat 启动服务。",
            )
        if not session_manager.is_ready:
            # 就绪标志过期时做一次真实探测（命中即刷新标志；失败由业务层自然报错）
            try:
                if not await session_manager.live_probe():
                    raise HTTPException(
                        status_code=503,
                        detail="会话暂不可用：上游未返回正常响应（可能被限流、验证过期或上游异常）。"
                               "请查看浏览器窗口：若显示 Cloudflare 验证页，手动完成一次即可；"
                               "否则稍后重试。",
                    )
            except HTTPException:
                raise
            except Exception:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail="无法连接浏览器窗口会话，请确认 Edge 窗口仍打开。",
                )
        return
    if not session_manager.is_ready:
        raise HTTPException(
            status_code=503,
            detail="站点会话尚未就绪（正在通过 Cloudflare 人机验证，首次约需 1-3 分钟），请稍后重试。",
        )


# ---- Web 面板（免鉴权静态资源；API 调用仍走各自端点鉴权）----
app = FastAPI(
    title=config.settings.APP_NAME,
    version=config.settings.APP_VERSION,
    description=(
        "aifreeforever.com API 反代网关：转换为 OpenAI / Anthropic 兼容接口。"
        "支持文本对话、图像生成、图像修改、文件上传与模型选择。"
    ),
    lifespan=lifespan,
    dependencies=[Depends(verify_api_key)],
)

# ---- Web 面板（静态资源；页面本身免鉴权，面板内 API 调用仍按各端点鉴权）----
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
if os.path.isdir(_WEB_DIR):
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=_WEB_DIR, html=True), name="panel")

    @app.get("/panel", include_in_schema=False)
    async def panel_redirect():
        return RedirectResponse("/ui/")


@app.get("/")
async def root():
    return {
        "app": config.settings.APP_NAME,
        "version": config.settings.APP_VERSION,
        "status": "ready" if session_manager.is_ready else "warming-up",
        "docs": "/docs",
        "panel": "/ui",
    }


@app.get("/health")
async def health():
    """健康状态。

    关于 session_ready：它表示「上游 API 当前是否可达」，**不等同于**「需要人机验证」。
    站点在 cookie 有效时直接可用、不会弹验证；只有当页面确实停在 Cloudflare
    挑战页时才需要手动过一次。详见 hint 字段。
    """
    ready = session_manager.is_ready
    alive = session_manager.is_alive() if config.settings.CDP_PORT > 0 else True
    if ready:
        hint = "运行正常，可直接调用接口。"
    elif not alive:
        hint = "浏览器窗口已关闭，请重新运行 start.bat。"
    else:
        hint = ("上游 API 暂不可达。请查看浏览器窗口：若显示 Cloudflare 验证页，"
                "手动完成一次即可（多数情况下无需验证，稍后重试也可能恢复）。")
    return {
        "status": "ok",
        "session_ready": ready,
        "browser_alive": alive,
        "models_cached": model_registry._cache is not None,
        "hint": hint,
    }


# ================= 会话 / cookie（API 直连通道） =================

@app.post("/admin/cookies")
async def import_cookies(request: Request):
    """导入用户浏览器手动过验证后导出的 cookie（API 直连通道的核心）。

    用法：在已通过人机验证的浏览器 DevTools Console 里执行：
      copy(document.cookie)
    然后 POST { "cookie": "<粘贴的 Cookie 字符串>" } 到本端点。
    """
    data = await request.json()
    cookie_str = data.get("cookie", "") or ""
    import http.cookies as _cookies

    parsed = _cookies.SimpleCookie()
    loaded = 0
    try:
        parsed.load(cookie_str)
        cookie_list = []
        domain = data.get("domain") or ".aifreeforever.com"
        for morsel in parsed.values():
            cookie_list.append(
                {"name": morsel.key, "value": morsel.value, "domain": domain, "path": "/"}
            )
            loaded += 1
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Cookie 解析失败: {e}")
    if loaded == 0:
        raise HTTPException(status_code=400, detail="没有解析到任何 cookie")

    session_manager.import_cookies(cookie_list)
    # 立即直连校验
    try:
        r = await session_manager.request_probe()
        return {
            "imported": loaded,
            "probe_status": r,
            "note": "若 probe 非 200，说明 cookie 过期或与当前出口 IP 不匹配，请重新获取",
        }
    except Exception as e:  # noqa: BLE001
        return {"imported": loaded, "probe_status": f"error: {e}"}


@app.get("/admin/cookies/status")
async def cookies_status():
    """查看当前 cookie 是否有效（直连探测）。"""
    try:
        r = await session_manager.request_probe()
        return {"probe_status": r, "cookie_count": len(session_manager._cookies)}
    except Exception as e:  # noqa: BLE001
        return {"probe_status": f"error: {e}", "cookie_count": len(session_manager._cookies)}


# ================= 聊天 =================

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    await check_ready()
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    return await chat_provider.chat_completion(data)


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    await check_ready()
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    return await chat_provider.anthropic_messages(data)


# ================= 模型 =================

@app.get("/v1/models")
async def list_models():
    return await model_registry.list_models()


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    data = await model_registry.list_models()
    for m in data["data"]:
        if m["id"] == model_id:
            return m
    raise HTTPException(status_code=404, detail="模型不存在")


# ================= 图像 =================

@app.post("/v1/images/generations")
async def image_generations(request: Request):
    await check_ready()
    data = await request.json()
    model = data.get("model") or config.settings.DEFAULT_IMAGE_MODEL
    prompt = data.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    try:
        n = int(data.get("n", 1) or 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="n 必须是正整数")
    n = max(1, min(n, 4))
    size = data.get("size", "1024x1024")
    response_format = data.get("response_format", "url")
    ratio = _ratio_from_size(size)

    results = []
    for _ in range(min(n, 4)):
        # 默认走"聊天内置文生图"通道（无需 turnstile token，但每 IP 每日限额）；
        # 仅当调用方显式指定了具体图像工作台模型时走 v2（需 turnstile，尽力自动采集）。
        if model_registry.is_image_model(model) and model != config.settings.DEFAULT_IMAGE_MODEL:
            r = await image_provider.generate_v2(model, prompt, ratio)
        else:
            r = await image_provider.generate_chatbot(prompt, ratio)
        results.append(r)
        if not r.get("ok"):
            break

    first = results[0]
    if not first.get("ok"):
        status = first.get("status", 502)
        msg = first.get("error") or first.get("head", "")[:200]
        raise HTTPException(
            status_code=502 if status in (500, -1) else status,
            detail=f"图像生成失败 (HTTP {status}): {msg}",
        )

    created = int(time.time())
    items = []
    for r in results:
        for u in (r.get("urls") or [])[:1]:
            if response_format == "b64_json":
                items.append({"b64_json": await image_provider.to_b64(u)})
            else:
                items.append({"url": u})
    return {"created": created, "data": items}


@app.post("/v1/images/edits")
async def image_edits(
    request: Request,
    image: UploadFile = File(...),
    prompt: str = Form(...),
    model: str = Form(config.settings.DEFAULT_IMAGE_MODEL),
    n: int = Form(1),
    size: str = Form("1024x1024"),
    response_format: str = Form("url"),
):
    await check_ready()
    raw = await image.read()
    ratio = _ratio_from_size(size)
    n = max(1, min(int(n or 1), 4))

    ok = await image_provider.moderate(raw)
    if not ok:
        raise HTTPException(status_code=400, detail="图片未通过上游合规检查")

    r = await image_provider.generate_v2(model, prompt, ratio, reference_images=[raw])
    if not r.get("ok"):
        status = r.get("status", 502)
        msg = r.get("head", "")[:300]
        raise HTTPException(
            status_code=502 if status in (500, -1) else status,
            detail=f"图像编辑失败 (HTTP {status}): {msg}",
        )
    created = int(time.time())
    items = []
    for u in (r.get("urls") or [])[: max(1, min(n, 4))]:
        if response_format == "b64_json":
            items.append({"b64_json": await image_provider.to_b64(u)})
        else:
            items.append({"url": u})
    return {"created": created, "data": items}


def _ratio_from_size(size: str) -> str:
    from app.formats import ratio_from_size

    return ratio_from_size(size, config.settings.DEFAULT_ASPECT_RATIO)


# ================= 文件 =================

@app.post("/v1/files")
async def upload_file(file: UploadFile = File(...), purpose: str = Form("assistants")):
    try:
        entry = await file_store.save(file, purpose)
    except ValueError as e:
        raise HTTPException(status_code=413, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.error("文件上传失败: %s", e)
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")
    return entry


@app.get("/v1/files")
async def list_files():
    return {"object": "list", "data": file_store.list()}


@app.get("/v1/files/{fid}")
async def get_file(fid: str):
    entry = file_store.get(fid)
    if not entry:
        raise HTTPException(status_code=404, detail="文件不存在")
    return entry


@app.get("/v1/files/{fid}/content")
async def file_content(fid: str):
    p = file_store.path(fid)
    if not p:
        raise HTTPException(status_code=404, detail="文件不存在")
    entry = file_store.get(fid)
    return FileResponse(p, media_type=entry["content_type"], filename=entry["filename"])


@app.delete("/v1/files/{fid}")
async def delete_file(fid: str):
    if not file_store.delete(fid):
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"id": fid, "object": "file", "deleted": True}


# ================= 视频 / 微调（上游未提供 → 501 说明） =================

@app.post("/v1/video")
async def video_generations(request: Request):
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": (
                    "上游 aifreeforever.com 当前未提供视频生成 API（其视频入口为 Kling AI 引流链接）。"
                    "文本/图像/图像修改/文件上传可用；视频生成建议接入支持视频的上游（如 fal.ai 等）。"
                ),
                "type": "not_supported",
                "code": 501,
            }
        },
    )


@app.post("/v1/fine_tuning/jobs")
async def fine_tuning_jobs(request: Request):
    return JSONResponse(
        status_code=501,
        content={
            "error": {
                "message": (
                    "上游 aifreeforever.com 当前未提供微调（fine-tuning）API。"
                    "本服务已提供 /v1/files 文件上传，可作为未来接入微调上游的数据通道。"
                ),
                "type": "not_supported",
                "code": 501,
            }
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=config.settings.HOST, port=config.settings.PORT, reload=False)