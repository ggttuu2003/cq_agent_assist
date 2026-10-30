"""应用共享依赖：数据库会话、当前个人资料和 LangGraph。"""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, Request
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import build_agent
from app.db.database import get_db
from app.db.models import User
from app.llm.errors import ModelConfigurationError
from app.llm.factory import create_chat_model
from app.services.conversations import get_user_conversation

Db = Annotated[AsyncSession, Depends(get_db)]


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
        return agents[model_name]
    # 只替换模型名，服务地址和密钥继续来自后端配置。
    settings = request.app.state.settings.model_copy(update={"llm_model": model_name})
    try:
        model = create_chat_model(settings, request.app.state.http_client)
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    agents[model_name] = build_agent(model)
    return agents[model_name]


async def get_agent(request: Request) -> CompiledStateGraph:
    """给单轮调试接口提供默认模型的图。"""
    return get_model_agent(request, request.app.state.settings.llm_model)


async def get_conversation_agent(
    conversation_id: UUID, request: Request, user: CurrentUser, db: Db,
) -> CompiledStateGraph:
    """核对会话归属后，按会话绑定的模型提供对应的图。"""
    conversation = await get_user_conversation(db, user.id, str(conversation_id))
    return get_model_agent(request, conversation.model_name)
