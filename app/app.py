from __future__ import annotations

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.staticfiles import NotModifiedResponse

from app.core.config import BASE_DIR, get_settings
from app.persistence.db import dialect_name, init_db, reset_engine
from app.persistence.checkpointer import reset_checkpointer, setup_checkpointer
from app.persistence.vectorstore.factory import reset_vector_store
from app.agent.graph import reset_fundamental_agent
from app.core.logging import setup_logging
from app.core.scheduler import start_scheduler, stop_scheduler
from app.routers.agent import router as agent_router
from app.routers.auth import router as auth_router
from app.routers.reports import router as reports_router
from app.routers.search import router as search_router
from app.routers.stock import router as stock_router
from app.services import reports as reports_svc
from app.services.universe import ensure_universe, universe_status
from app.services.users import bootstrap_admin_if_needed

logger = logging.getLogger(__name__)

# Windows 常把 .js 标成 text/plain，导致 type="module" 脚本被浏览器拒绝执行
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

STATIC_DIR = BASE_DIR / "static"


class FixedStaticFiles(StaticFiles):
    """确保 .js / .css 使用浏览器可执行的 MIME（修复 Windows 下 text/plain）。"""

    def file_response(self, full_path, stat_result, scope, status_code: int = 200):
        path_str = str(full_path).replace("\\", "/").lower()
        media_type = None
        if path_str.endswith(".js"):
            media_type = "application/javascript"
        elif path_str.endswith(".css"):
            media_type = "text/css"

        request_headers = Headers(scope=scope)
        response = FileResponse(
            full_path,
            status_code=status_code,
            stat_result=stat_result,
            media_type=media_type,
        )
        # 开发期避免 CSS/JS 被强缓存导致改了格式仍显示旧结果
        if path_str.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        if self.is_not_modified(response.headers, request_headers):
            return NotModifiedResponse(response.headers)
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    settings = get_settings()
    logger.info(
        "lifespan startup begin debug=%s auth_enabled=%s",
        settings.debug,
        settings.auth_enabled,
    )
    if settings.auth_enabled:
        settings.require_auth_secret()
        logger.info("auth enabled; JWT secret configured")
    else:
        logger.info("auth disabled; APIs are open")

    logger.info("init database schema…")
    await init_db()
    logger.info("database schema ready dialect=%s", dialect_name())

    logger.info("bootstrap auth user (if configured)…")
    await bootstrap_admin_if_needed()

    logger.info("setup checkpointer…")
    await setup_checkpointer()
    logger.info("checkpointer ready")

    # --reload / 崩溃后内存任务丢失，避免历史页假死在「分析中」
    logger.info("recover orphan running reports…")
    orphans = await reports_svc.fail_orphan_running_reports()
    logger.info("orphan recovery done count=%s ids=%s", len(orphans), orphans)

    logger.info("start scheduler…")
    start_scheduler()
    logger.info("scheduler started")

    async def _warmup():
        logger.info("universe warmup begin…")
        try:
            await ensure_universe()
            st = await universe_status()
            logger.info("universe warmup done status=%s", st)
        except Exception as exc:
            logger.warning("universe warmup failed: %s", exc)

    task = asyncio.create_task(_warmup())
    logger.info("lifespan startup complete; serving requests")
    try:
        yield
    finally:
        logger.info("lifespan shutdown begin…")
        if not task.done():
            task.cancel()
            logger.info("cancelled universe warmup task")
        stop_scheduler()
        logger.info("scheduler stopped")
        try:
            orphans = await reports_svc.fail_orphan_running_reports(
                "服务正在关闭或重载，分析已中断（可重跑）"
            )
            logger.info(
                "shutdown orphan recovery count=%s ids=%s", len(orphans), orphans
            )
        except Exception as exc:
            logger.warning("shutdown orphan report recovery failed: %s", exc)
        try:
            await reset_engine()
            logger.info("database engine disposed")
        except Exception as exc:
            logger.warning("shutdown engine dispose failed: %s", exc)
        try:
            reset_fundamental_agent()
            reset_vector_store()
            logger.info("agent / vector store reset")
        except Exception as exc:
            logger.warning("shutdown vector/docstore reset failed: %s", exc)
        try:
            await reset_checkpointer()
            logger.info("checkpointer closed")
        except Exception as exc:
            logger.warning("shutdown checkpointer reset failed: %s", exc)
        logger.info("lifespan shutdown complete")


def create_app():
    settings = get_settings()
    # 正式环境 DEBUG=false：关闭 Swagger / ReDoc / OpenAPI JSON
    docs_url = "/docs" if settings.debug else None
    redoc_url = "/redoc" if settings.debug else None
    openapi_url = "/openapi.json" if settings.debug else None
    app = FastAPI(
        title="A股深研 API",
        description="个股深研工作台：可切换数据库的股票池 + 财务摘要 + Deep Agents 多智能体分析（SSE）",
        version="1.9.0",
        lifespan=lifespan,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    logger.info(
        "fastapi docs %s (DEBUG=%s)",
        "enabled" if settings.debug else "disabled",
        settings.debug,
    )

    app.include_router(auth_router)
    app.include_router(search_router)
    app.include_router(stock_router)
    app.include_router(agent_router)
    app.include_router(reports_router)
    app.mount("/static", FixedStaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC_DIR / "favicon.ico")

    @app.get("/health")
    async def health():
        return {"status": "ok", "universe": await universe_status()}

    @app.get("/api/ui-config", include_in_schema=False)
    async def ui_config():
        """前端壳层配置（无需登录）。"""
        s = get_settings()
        return {
            "debug": bool(s.debug),
            "show_api_docs": bool(s.debug),
            "auth_enabled": bool(s.auth_enabled),
        }

    return app
