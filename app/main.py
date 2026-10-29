"""FastAPI 应用入口。"""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.core.settings import get_settings


settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用生命周期：启动时准备共享对象，关闭时释放网络连接。"""
    # app.state 相当于应用自己的公共存储，接口可以通过 request.app.state 访问。
    app.state.settings = settings
    # 暂不创建图，第一次调用 /chat 时由 get_agent 创建。
    app.state.agent = None
    async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
        app.state.http_client = client
        # yield 前是启动逻辑；应用关闭后离开 async with，客户端会自动释放连接。
        yield


# 创建应用，并注册各模块的接口，具体接口逻辑放在 api 子包。
app = FastAPI(title="CQ Agent Assist", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(health_router)
