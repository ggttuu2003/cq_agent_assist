"""应用共享依赖：数据库会话、当前个人资料和 LangGraph。"""

from typing import Annotated
import logging
from uuid import UUID

import anyio
from fastapi import Depends, HTTPException, Request
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import build_agent
from app.db.database import get_db
from app.db.models import User
from app.llm.errors import ModelConfigurationError
from app.llm.factory import create_chat_model
from app.search.factory import create_search_provider
from app.schemas.conversations import SendMessageRequest
from app.services.conversations import Generation, prepare_generation, release_generation
from app.tools.web_search import web_search

Db = Annotated[AsyncSession, Depends(get_db)]
logger = logging.getLogger(__name__)


async def get_current_user(db: Db) -> User:
    """读取固定的本地个人档案，不接收前端传来的用户编号。"""
    user = await db.get(User, 1)
    if user is None:
        raise HTTPException(409, "个人资料尚未初始化")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_model_agent(request: Request, model_name: str) -> CompiledStateGraph:
    """按模型名复用对话图；每张图绑定自己的模型客户端。"""
    model_name = model_name.strip()
    agents = request.app.state.agents
    if model_name in agents:
        logger.debug("复用已缓存的对话图 模型=%s", model_name)
        return agents[model_name]
    # 只替换模型名，服务地址和密钥继续来自后端配置。
    settings = request.app.state.settings.model_copy(update={"llm_model": model_name})
    try:
        model = create_chat_model(settings, request.app.state.http_client)
        provider = create_search_provider(settings, request.app.state.http_client)
    except ModelConfigurationError as exc:
        logger.warning("创建对话图失败 模型=%s 原因=%s", model_name, exc)
        raise HTTPException(status_code=503, detail=str(exc)) from None
    tools = [web_search] if provider else []
    # with_config 给这张图附加后台运行配置，执行工具时通过 get_config() 读取。
    # 服务对象不出现在模型的工具参数中，也不使用所有图共享的全局可变变量。
    agents[model_name] = build_agent(model, tools).with_config(
        {"configurable": {"search_provider": provider}},
    )
    logger.info("对话图创建完成 模型=%s 搜索服务=%s", model_name,
                type(provider).__name__ if provider else "未启用")
    return agents[model_name]


async def get_agent(request: Request) -> CompiledStateGraph:
    """给单轮调试接口提供默认模型的图。"""
    return get_model_agent(request, request.app.state.settings.llm_model)


async def get_generation(
    conversation_id: UUID, body: SendMessageRequest, request: Request, user: CurrentUser, db: Db,
) -> Generation:
    """先领取生成占用，再选择模型，防止切换模型与发送消息互相穿插。"""
    return await prepare_generation(db, user.id, str(conversation_id), body,
                                    request.app.state.settings.llm_timeout)


# 同一请求中 FastAPI 会缓存依赖返回值，接口与选图依赖共用一次准备结果。
PreparedGeneration = Annotated[Generation, Depends(get_generation)]


async def get_conversation_agent(
    request: Request, generation: PreparedGeneration, db: Db,
) -> CompiledStateGraph | None:
    """按本轮已锁定的模型选图；重放已保存回答不需要创建模型客户端。"""
    if generation.saved:
        return None
    try:
        return get_model_agent(request, generation.model_name)
    except BaseException:
        # 配置错误发生在领取占用之后，也要释放标记，避免会话一直显示正在回答。
        with anyio.CancelScope(shield=True):
            await release_generation(db, generation.user_id, generation.conversation_id, generation.lease)
        raise
