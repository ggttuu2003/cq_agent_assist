"""负责会话归属、上下文、重复请求和问答持久化。"""

import asyncio
from datetime import timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Conversation, Message, utc_now
from app.llm.errors import ModelError
from app.schemas.conversations import ConversationInfo, MessageInfo, SendMessageRequest


def conversation_info(conversation: Conversation) -> ConversationInfo:
    """从已加载的消息构造接口结果，避免异步 ORM 隐式查询。"""
    return ConversationInfo(
        id=conversation.id, title=conversation.title,
        model_name=conversation.model_name,
        updated_at=conversation.updated_at.replace(tzinfo=timezone.utc),
        generating=bool(conversation.busy_until and conversation.busy_until > utc_now()),
        messages=[MessageInfo(id=m.id, role=m.role, content=m.content, model=m.model,
                              request_id=m.request_id) for m in conversation.messages],
    )


async def list_user_conversations(db: AsyncSession, user_id: int) -> list[Conversation]:
    """按更新时间读取当前个人资料的会话和消息。"""
    rows = await db.scalars(select(Conversation).where(
        Conversation.user_id == user_id,
    ).options(selectinload(Conversation.messages)).order_by(
        Conversation.updated_at.desc(), Conversation.id,
    ))
    return list(rows)


async def create_user_conversation(db: AsyncSession, user_id: int, model_name: str,
                                   first_message: str) -> Conversation:
    """创建时保存首次问题前 28 个字符作为标题，不依赖模型返回。"""
    conversation = Conversation(id=str(uuid4()), user_id=user_id, model_name=model_name,
                                title=first_message[:28])
    db.add(conversation)
    await db.commit()
    return await get_user_conversation(db, user_id, conversation.id)


async def delete_user_conversation(db: AsyncSession, conversation_id: str) -> None:
    """删除会话，消息通过外键级联删除。"""
    await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await db.commit()


async def rename_user_conversation(db: AsyncSession, user_id: int, conversation_id: str,
                                   title: str) -> Conversation:
    """加锁修改当前用户的会话标题，避免与正在生成的回答互相覆盖。"""
    await db.commit()
    conversation = await owned_conversation(db, user_id, conversation_id, lock=True)
    if conversation.busy_until and conversation.busy_until > utc_now():
        raise HTTPException(409, "会话正在回答，请完成后再修改标题")
    conversation.title = title
    conversation.updated_at = utc_now()
    await db.commit()
    return await get_user_conversation(db, user_id, conversation_id)


async def owned_conversation(db: AsyncSession, user_id: int, conversation_id: str, *, lock=False):
    """只读取属于当前个人档案的会话；没有匹配记录时返回 404。"""
    query = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.user_id == user_id,
    ).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    else:
        query = query.options(selectinload(Conversation.messages)).execution_options(populate_existing=True)
    conversation = await db.scalar(query)
    if not conversation:
        raise HTTPException(404, "会话不存在")
    return conversation


async def get_user_conversation(db: AsyncSession, user_id: int, conversation_id: str, *, lock=False):
    """对外提供带归属校验的会话查询。"""
    return await owned_conversation(db, user_id, conversation_id, lock=lock)


def model_history(messages: list[Message]) -> list[tuple[str, str]]:
    """最多保留最近 20 轮，并按完整问答控制历史字符总量。"""
    rounds = []
    total = 0
    for index in range(len(messages) - 2, -1, -2):
        pair = messages[index:index + 2]
        size = sum(len(message.content) for message in pair)
        if total + size > 60000:
            break
        rounds.insert(0, [(message.role, message.content) for message in pair])
        total += size
    return [message for pair in rounds for message in pair]


async def release_generation(db: AsyncSession, user_id: int, conversation_id: str, lease: str):
    """只释放自己持有的生成标记，过期的请求不能影响新的生成。"""
    await db.rollback()
    conversation = await db.scalar(select(Conversation).where(
        Conversation.id == conversation_id, Conversation.user_id == user_id,
    ).with_for_update())
    if conversation and conversation.active_request == lease:
        conversation.active_request = None
        conversation.busy_until = None
    await db.commit()


async def answer_message(db: AsyncSession, user_id: int, conversation_id: str,
                         body: SendMessageRequest, agent, timeout: float):
    """短事务领取占用，事务外调用图，再用短事务保存完整问答。"""
    request_id = str(body.request_id)
    # 读取个人资料会开启只读事务，先结束它，保证下一次加锁读取最新会话。
    await db.commit()
    conversation = await owned_conversation(db, user_id, conversation_id, lock=True)
    # 模型从会话读取，避免发送过程中被全局默认配置悄悄替换。
    model_name = conversation.model_name
    previous = list((await db.scalars(select(Message).where(
        Message.conversation_id == conversation_id, Message.request_id == request_id,
    ).order_by(Message.sequence))).all())
    if previous:
        if previous[0].content != body.message:
            raise HTTPException(409, "同一个请求编号不能用于不同的问题")
        await db.commit()
        return conversation_info(await owned_conversation(db, user_id, conversation_id))
    if conversation.busy_until and conversation.busy_until > utc_now():
        raise HTTPException(409, "这段会话正在回答，请稍候再试")
    lease = str(uuid4())
    conversation.active_request = lease
    # 客户端可能重试模型请求；整次图调用也有明确的总时限。
    duration = max(10, timeout * 3)
    conversation.busy_until = utc_now() + timedelta(seconds=duration + 30)
    history_rows = list((await db.scalars(select(Message).where(
        Message.conversation_id == conversation_id,
    ).order_by(Message.sequence.desc()).limit(40))).all())[::-1]
    history = model_history(history_rows)
    sequence = history_rows[-1].sequence if history_rows else 0
    await db.commit()

    try:
        async with asyncio.timeout(duration):
            result = await agent.ainvoke({"messages": [*history, ("user", body.message)]})
        reply = result["messages"][-1].text
        if not reply.strip():
            raise ModelError("模型返回了空回答，请重试")
        conversation = await owned_conversation(db, user_id, conversation_id, lock=True)
        if conversation.active_request != lease:
            raise HTTPException(409, "本次请求已过期，请重新打开会话")
        conversation.updated_at = utc_now()
        conversation.active_request = None
        conversation.busy_until = None
        db.add_all([
            Message(id=str(uuid4()), conversation_id=conversation_id, sequence=sequence + 1,
                    request_id=request_id, role="user", content=body.message),
            Message(id=str(uuid4()), conversation_id=conversation_id, sequence=sequence + 2,
                    request_id=request_id, role="assistant", content=reply, model=model_name),
        ])
        await db.commit()
    except BaseException:
        # 包括页面断开导致的任务取消；仍需撤销当前请求占用。
        await release_generation(db, user_id, conversation_id, lease)
        raise
    return conversation_info(await owned_conversation(db, user_id, conversation_id))
