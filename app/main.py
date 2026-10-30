"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.profile import router as profile_router
from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.health import router as health_router
from app.core.settings import get_settings
from app.db.database import create_database, initialize_database


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用生命周期：启动时准备共享对象，关闭时释放网络连接。"""
    # app.state 相当于应用自己的公共存储，接口可以通过 request.app.state 访问。
    app.state.settings = settings
    # 按模型名缓存流程图，第一次使用某个模型时再创建。
    app.state.agents = {}
    engine, app.state.db_sessions = create_database(settings)
    try:
        try:
            await initialize_database(engine, settings.llm_model)
        except SQLAlchemyError:
            raise RuntimeError("无法初始化 MySQL，请检查 .env 连接配置，并确认目标数据库已创建") from None
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            app.state.http_client = client
            yield
    finally:
        await engine.dispose()


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
            return JSONResponse({"detail": "请求来源不受信任"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, exc: SQLAlchemyError):
    """数据库故障不给浏览器返回 SQL、密码或内部连接信息。"""
    return JSONResponse({"detail": "数据库暂时不可用，请稍后重试"}, status_code=503)


app.include_router(profile_router)
app.include_router(conversations_router)
app.include_router(chat_router)
app.include_router(health_router)
