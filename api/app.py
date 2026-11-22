"""FastAPI ilovasi: marshrutlar, middleware, xato konvertlari, Mini App.

Bu modul **yagona HTTP entry point**:

* ``GET /`` — Render/PaaS health-check'i (``"OK"`` qaytaradi) yoki ``Accept:
  application/json`` bo'lsa JSON holat;
* ``{API_PREFIX}/...`` — barcha REST marshrutlari (auth, files, folders, shares,
  stream, system);
* ``{WEB_MOUNT_PATH}`` — Telegram Mini App (``web/`` papkasidagi statik fayllar),
  SPA uchun fallback bilan;
* ``/docs`` va ``/openapi.json`` — interaktiv hujjatlar (``API_DOCS_ENABLED``).

Lifespan (ishga tushish / o'chish) barcha infratuzilmani boshqaradi:

1. DB (``init_db``) va ``system_flags`` dan jonli sozlamalar;
2. Redis kesh (yo'q bo'lsa — in-memory fallback);
3. Rate limiter;
4. ``httpx.AsyncClient`` — stream proxy uchun (Telegram'ga Range so'rovlari);
5. Bot obyekti + arxiv vaulti (agar bot tokeni bo'lsa) — ``/stream`` va
   ``recover_file`` uchun kerak.

Xatolar yagona konvertda qaytadi::

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from api.routes import (
    auth_router,
    files_router,
    folders_router,
    shares_router,
    stream_router,
    system_router,
)
from api.routes.system import mark_started
from config import Settings, get_settings
from services.exceptions import AppError, DomainError

logger = logging.getLogger(__name__)

__all__ = ["app", "create_app"]

#: Loyilha ildizi (``web/`` papkasini topish uchun).
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Infratuzilmani ishga tushirish va toza o'chirish."""
    settings: Settings = application.state.settings
    mark_started()
    started = time.time()

    # 1) Ma'lumotlar bazasi.
    from db.database import dispose_engine, init_db

    await init_db()

    # 2) Jonli bayroqlar (``/diyor`` o'zgartirgan sozlamalar).
    try:
        from bot.setup import apply_runtime_flags

        await apply_runtime_flags(settings)
    except Exception as exc:
        logger.info("Runtime flags skipped: %s", exc)

    # 2b) MTProto klientini oldindan ulash (fon vazifasi): birinchi katta fayl
    #     ochilishida Telethon ulanishi (~0.4-1 s) kutib qolmasin.
    if settings.mt.configured:

        async def _warm_mtproto() -> None:
            try:
                from services.mtproto import get_client

                logger.info("MTProto client warmed: %s", bool(await get_client(settings)))
            except Exception as exc:  # noqa: BLE001 - ixtiyoriy optimizatsiya
                logger.warning("MTProto warm-up failed: %s", exc)

        application.state.mt_warm_task = asyncio.create_task(_warm_mtproto())

    # 3) Kesh va rate limiter.
    from services.cache import close_cache, init_cache
    from services.rate_limiter import close_rate_limiter, init_rate_limiter

    cache = await init_cache()
    limiter = await init_rate_limiter()

    # 4) Stream proxy uchun umumiy HTTP klient.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.api.request_timeout, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        follow_redirects=True,
    )

    # 5) Bot + arxiv vaulti (ixtiyoriy: API'ni botsiz ham ishga tushirish mumkin).
    bot = None
    try:
        from bot.setup import build_bot, install_vault

        bot = build_bot(settings)
        install_vault(bot, settings)
        application.state.bot = bot
    except Exception as exc:
        logger.warning("Bot is not available in the API process: %s", exc)

    application.state.http = client
    application.state.cache = cache
    application.state.limiter = limiter
    logger.info(
        "API ready in %.2fs (prefix=%s, web=%s, stream=%s)",
        time.time() - started,
        settings.api.api_prefix,
        settings.api.serve_web,
        settings.storage.stream_proxy_enabled,
    )

    try:
        yield
    finally:
        await client.aclose()
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                logger.debug("Bot session close failed", exc_info=True)
        await close_rate_limiter()
        await close_cache()
        await dispose_engine()
        logger.info("API shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Ilovani yig'ish (testlar ham shu fabrikadan foydalanadi)."""
    settings = settings or get_settings()

    application = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        description=(
            "Telegram Cloud Storage — fayllar Telegram serverlarida saqlanadi, "
            "bu servis faqat metadata, ulashish va media stream bilan ishlaydi."
        ),
        docs_url="/docs" if settings.api.docs_enabled else None,
        redoc_url="/redoc" if settings.api.docs_enabled else None,
        openapi_url="/openapi.json" if settings.api.docs_enabled else None,
        lifespan=lifespan,
    )
    application.state.settings = settings

    # --- middleware -----------------------------------------------------------
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_credentials="*" not in settings.api.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "RateLimit-Reset",
            "Content-Range",
            "Accept-Ranges",
        ],
    )
    application.add_middleware(_RequestTimingMiddleware)
    # Siqish (gzip): Mini App HTML ~436 KB -> ~112 KB, three.js 615 -> 151 KB.
    # Tunnel orqali birinchi ochilish shu tufayli bir necha barobar tezlashadi.
    application.add_middleware(_SelectiveGZipMiddleware)
    application.add_middleware(_VendorCacheMiddleware)

    # --- xato konvertlari -----------------------------------------------------
    @application.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        """Biznes xatolari (404/403/409/410/423 …) — yagona konvert."""
        del request
        return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict()})

    @application.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        """Infrastruktura xatolari (Telegram API va h.k.)."""
        del request
        return JSONResponse(
            status_code=getattr(exc, "status_code", 502),
            content={"error": exc.to_dict()},
        )

    @application.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic validatsiya xatolari — 422 va aniq maydonlar ro'yxati."""
        del request
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request body or query is invalid",
                    "details": {"errors": exc.errors()[:20]},
                }
            },
        )

    # --- marshrutlar ----------------------------------------------------------
    prefix = settings.api.api_prefix
    application.include_router(system_router, prefix=prefix)
    application.include_router(auth_router, prefix=prefix)
    application.include_router(files_router, prefix=prefix)
    application.include_router(folders_router, prefix=prefix)
    application.include_router(shares_router, prefix=prefix)
    application.include_router(stream_router, prefix=prefix)

    # --- Mini App (statik fayllar) -------------------------------------------
    # Ildiz marshrutidan OLDIN ulaymiz: ``_mount_web`` ``index.html`` yo'lini
    # ``application.state`` ga yozadi va ``GET /`` shu faylni brauzerga beradi.
    _mount_web(application, settings)

    @application.get("/", include_in_schema=False)
    async def root(request: Request) -> Any:
        """Ildiz marshrut: Mini App **va** health-check bir joyda.

        Uch xil mijoz keladi:

        1. **Telegram / brauzer** (``Accept: text/html``) — Mini App sahifasi;
        2. **monitoring** (``Accept: application/json`` yoki ``?format=json``) —
           batafsil sog'liq holati;
        3. **PaaS health-check** (``curl``) — legacy'dagi kabi ``"OK"`` matni
           (yoki Mini App HTML'i — ikkalasi ham ``200``, shuning uchun
           health-check o'tadi).

        Aniq ``"OK"`` matni kerak bo'lsa ``GET /healthz`` dan foydalaning.
        """
        wants_json = (
            request.query_params.get("format") == "json"
            or "application/json" in (request.headers.get("accept") or "")
        )
        if wants_json:
            from api.routes.system import health

            return await health(settings)

        index_file = getattr(application.state, "index_file", None)
        if index_file is not None:
            accept = request.headers.get("accept") or ""
            # Monitoring agentlari odatda ``*/*`` yuboradi; brauzer va Telegram
            # esa ``text/html`` ni birinchi qo'yadi.
            if "text/html" in accept:
                return FileResponse(index_file, headers=NO_CACHE_HEADERS)
        return PlainTextResponse("OK")

    @application.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        """Docker/Kubernetes/PaaS sog'liq tekshiruvi: har doim oddiy ``"OK"``."""
        return PlainTextResponse("OK")

    return application


#: Mini App HTML uchun kesh siyosati. Telegram WebView va brauzer eski
#: ``index.html`` nusxasini ushlab qolmasligi kerak: har yangilanish darhol
#: yetib borishi uchun qayta tekshirish majburiy (ETag/Last-Modified saqlanadi).
NO_CACHE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}


def _mount_web(application: FastAPI, settings: Settings) -> None:
    """``web/`` papkasini Mini App sifatida ulash (SPA fallback bilan).

    ``index.html`` yo'li ``application.state.index_file`` ga yoziladi — ``GET /``
    marshruti Telegram/brauzerga aynan shu faylni qaytaradi.
    """
    application.state.index_file = None
    if not settings.api.serve_web:
        logger.info("Mini App disabled (SERVE_WEB=false): / returns plain OK")
        return

    static_dir = PROJECT_ROOT / settings.web.static_dir
    index_file = static_dir / "index.html"
    if not index_file.exists():
        logger.info("Mini App not served: %s is missing", index_file)
        return

    application.state.index_file = index_file

    mount_path = settings.web.mount_path.rstrip("/") or ""

    @application.get("/s/{token}", include_in_schema=False)
    async def share_page(token: str) -> FileResponse:
        """Ommaviy ulashish sahifasi (``{BASE_URL}/s/<token>``).

        Bu SPA marshruti: barcha ma'lumotlar ``/api/v1/shares/public/<token>``
        orqali olinadi, shuning uchun server faqat ``index.html`` ni beradi.
        """
        del token
        return FileResponse(index_file, headers=NO_CACHE_HEADERS)

    @application.get("/app/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str) -> FileResponse:
        """Mini App ichki marshrutlari (``/app/files``, ``/app/settings`` …)."""
        candidate = (static_dir / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(static_dir.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_file, headers=NO_CACHE_HEADERS)

    if mount_path and mount_path not in {"", "/app"}:
        application.mount(mount_path, StaticFiles(directory=str(static_dir), html=True), name="web")
    @application.get("/index.html", include_in_schema=False)
    async def index_page() -> FileResponse:
        """Mini App'ni aniq manzildan ochish (``/index.html``)."""
        return FileResponse(index_file, headers=NO_CACHE_HEADERS)

    application.mount(
        "/static", StaticFiles(directory=str(static_dir), html=False), name="static"
    )
    logger.info("Mini App mounted from %s (mount_path=%s)", static_dir, mount_path or "/")


class _SelectiveGZipMiddleware:
    """Faqat matnli javoblarni siqish; media oqimiga TEGMASLIK.

    Nega oddiy ``GZipMiddleware`` emas: ``/stream`` va ``/download`` video/audio
    ni ``Range`` (206) bilan beradi — siqilsa ``Content-Length``/``Content-Range``
    buziladi va pleerda seek ishlamaydi. Shuning uchun media yo'llari va
    ``Range`` so'rovlari to'g'ridan-to'g'ri o'tkaziladi.
    """

    _SKIP_PARTS = ("/stream", "/download", "/thumb", "/upload")

    def __init__(self, app: Any) -> None:
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=6)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        headers = dict(scope.get("headers") or [])
        if b"range" in headers or any(part in path for part in self._SKIP_PARTS):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


class _VendorCacheMiddleware:
    """``/static/vendor/*`` uchun uzoq kesh (fayl nomlari versiyali: three.r134 …)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/static/vendor/"):
            await self.app(scope, receive, send)
            return

        async def send_with_cache(message: Any) -> None:
            if message.get("type") == "http.response.start" and message.get("status") == 200:
                headers = [h for h in message.get("headers", []) if h[0].lower() != b"cache-control"]
                headers.append((b"cache-control", b"public, max-age=604800"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cache)


class _RequestTimingMiddleware:
    """Har javobga ``X-Process-Time`` qo'shish (ASGI middleware)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                elapsed = (time.perf_counter() - started) * 1000
                headers = message.setdefault("headers", [])
                headers.append((b"x-process-time", f"{elapsed:.1f}ms".encode()))
            await send(message)

        await self.app(scope, receive, send_wrapper)


#: Uvicorn/Gunicorn import qiladigan modul darajasidagi ilova.
app = create_app()
