"""会话接口，只负责路由参数和 HTTP 响应。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from langgraph.graph.state import CompiledStateGraph

from app.dependencies import CurrentUser, Db, get_conversation_agent
from app.db.models import utc_now
from app.llm.errors import ModelError
from app.schemas.conversations import (
    ConversationInfo, ConversationTitleUpdate, CreateConversationRequest, SendMessageRequest,
)
from app.services.conversations import (
    answer_message,
    conversation_info,
    create_user_conversation,
    delete_user_conversation,
    get_user_conversation,
    list_user_conversations,
    prepare_generation,
    rename_user_conversation,
)
from app.services.streaming import ConversationStreamResponse

router = APIRouter(prefix="/conversations", tags=["会话"])


@router.get("", response_model=list[ConversationInfo])
async def list_conversations(user: CurrentUser, db: Db):
    """返回当前个人资料下的会话。"""
    return [conversation_info(item) for item in await list_user_conversations(db, user.id)]


@router.post("", response_model=ConversationInfo, status_code=201)
async def create_conversation(body: CreateConversationRequest, request: Request,
                              user: CurrentUser, db: Db):
    """根据首次问题创建会话并保存标题，此时不调用模型。"""
    # 新会话绑定创建时的默认模型；已有会话不会随全局配置变化。
    model_name = request.app.state.settings.llm_model.strip()
    if not model_name:
        raise HTTPException(503, "请先配置新会话的默认模型 LLM_MODEL")
    return conversation_info(await create_user_conversation(db, user.id, model_name, body.first_message))


@router.get("/{conversation_id}", response_model=ConversationInfo)
async def get_conversation(conversation_id: UUID, user: CurrentUser, db: Db):
    """返回当前个人资料的一段会话。"""
    return conversation_info(await get_user_conversation(db, user.id, str(conversation_id)))


@router.patch("/{conversation_id}", response_model=ConversationInfo)
async def rename_conversation(conversation_id: UUID, body: ConversationTitleUpdate,
                              user: CurrentUser, db: Db):
    """保存用户修改的会话标题。"""
    return conversation_info(await rename_user_conversation(
        db, user.id, str(conversation_id), body.title,
    ))


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: UUID, user: CurrentUser, db: Db):
    """删除会话及其消息。"""
    conversation = await get_user_conversation(db, user.id, str(conversation_id), lock=True)
    if conversation.busy_until and conversation.busy_until > utc_now():
        raise HTTPException(409, "会话正在回答，请完成后再删除")
    await delete_user_conversation(db, conversation.id)
    return Response(status_code=204)


@router.post("/{conversation_id}/messages", response_model=ConversationInfo)
async def send_message(conversation_id: UUID, body: SendMessageRequest, request: Request,
                       user: CurrentUser, db: Db,
                       agent: Annotated[CompiledStateGraph, Depends(get_conversation_agent)]):
    """提交消息，业务层负责读取上下文、调用图并保存结果。"""
    try:
        return await answer_message(db, user.id, str(conversation_id), body, agent,
                                    request.app.state.settings.llm_timeout)
    except ModelError as exc:
        raise HTTPException(502, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, "回答超时，请稍后重试") from None


@router.post("/{conversation_id}/messages/stream", response_class=ConversationStreamResponse, status_code=200)
async def stream_message(conversation_id: UUID, body: SendMessageRequest, request: Request,
                         user: CurrentUser, db: Db,
                         agent: Annotated[CompiledStateGraph, Depends(get_conversation_agent)]):
    """先检查会话是否存在、请求是否重复，以及该会话是否正在回答。

    允许开始新的回答时，在数据库中标记“该会话正在回答”，再开始流式返回。
    这样校验失败仍能返回正常的 HTTP 错误，而不是先返回 200 再报告错误。
    等待模型回答期间不一直占用数据库连接。
    """
    generation = await prepare_generation(db, user.id, str(conversation_id), body,
                                          request.app.state.settings.llm_timeout)
    return ConversationStreamResponse(request.app.state.db_sessions, generation, agent)
