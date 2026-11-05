"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager, suppress
import asyncio
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.traces import router as traces_router
from app.api.knowledge import router as knowledge_router
from app.services.knowledge import KnowledgeService
from app.knowledge.worker import KnowledgeWorker
from app.api.profile import router as profile_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.api.attachments import router as attachments_router
from app.core.settings import get_settings
from app.core.logging import RequestLoggingMiddleware, configure_logging
from app.db.database import create_database, initialize_database


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用生命周期：启动时准备共享对象，关闭时释放网络连接。"""
    logger.info("应用启动 环境=%s 默认模型=%s 搜索服务=%s", settings.app_env,
                settings.llm_model or "未配置", settings.search_provider)
    # app.state 相当于应用自己的公共存储，接口可以通过 request.app.state 访问。
    app.state.settings = settings
    # 按模型名缓存流程图，第一次使用某个模型时再创建。
    app.state.agents = {}
    engine, app.state.db_sessions = create_database(settings)
    try:
        try:
            await initialize_database(engine, settings.llm_model)
        except SQLAlchemyError:
            logger.error("MySQL 初始化失败，请检查连接配置和数据库状态", exc_info=True)
            raise RuntimeError("无法初始化 MySQL，请检查 .env 连接配置，并确认目标数据库已创建") from None
        logger.info("MySQL 初始化完成")
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            app.state.http_client = client
            app.state.knowledge = KnowledgeService(settings, app.state.db_sessions, client)
            # 队列保存在 MySQL；应用重启可以恢复，多个进程用租约避免重复执行。
            worker = asyncio.create_task(KnowledgeWorker(app.state.knowledge).run())
            logger.info("应用就绪，知识资料处理器已启动")
            try:
                yield
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
                await app.state.knowledge.close()
    finally:
        await engine.dispose()
        logger.info("应用关闭，数据库和网络连接已释放")


# 创建应用，并注册只包含路由的 api 子包。
app = FastAPI(title="CQ Agent Assist", lifespan=lifespan)


@app.middleware("http")
async def protect_local_requests(request: Request, call_next):
    """限制带浏览器来源的写请求，防止其他网站直接操作本地个人服务。"""
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/")}
        if settings.app_env == "dev":
            allowed.update({"http://127.0.0.1:5173", "http://localhost:5173"})
        if (origin and origin not in allowed) or request.headers.get("sec-fetch-site") == "cross-site":
            logger.warning("拒绝不受信任来源的写请求")
            return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, exc: SQLAlchemyError):
    """数据库故障不给浏览器返回 SQL、密码或内部连接信息。"""
    logger.error("数据库操作失败，返回 HTTP 503", exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse({"detail": "数据库暂时不可用，请稍后重试"}, status_code=503)


app.include_router(profile_router)
app.include_router(conversations_router)
app.include_router(chat_router)
app.include_router(health_router)
app.include_router(attachments_router)
app.include_router(knowledge_router)
app.include_router(traces_router)

# 使用 ASGI 中间件包住完整响应，避免把“响应头已返回”当作流式生成完成。
app.add_middleware(RequestLoggingMiddleware)
