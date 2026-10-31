"""负责会话归属、上下文、重复请求和问答持久化。"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from time import perf_counter
from uuid import uuid4

import anyio
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Conversation, Message, MessageAttachment, utc_now
from app.llm.errors import ModelError
from app.services.knowledge import validate_citations
from app.llm.factory import image_input_support
from app.schemas.conversations import ConversationInfo, ConversationKnowledgeInfo, ConversationModelInfo, ConversationTurnInfo, MessageInfo, SendMessageRequest
from app.services.attachments import (
    MAX_IMAGES, MAX_IMAGE_TOTAL_SIZE, attachment_info, get_attachments, message_content,
)

logger = logging.getLogger(__name__)


def message_info(message: Message) -> MessageInfo:
    """消息正文与附件描述一起返回，图片内容由独立接口读取。"""
    return MessageInfo(id=message.id, role=message.role, content=message.content, model=message.model,
                       request_id=message.request_id,
                       attachments=[attachment_info(item) for item in message.attachments], citations=message.citations or [])


def conversation_info(conversation: Conversation) -> ConversationInfo:
    """从已加载的消息构造接口结果，避免异步 ORM 隐式查询。"""
    return ConversationInfo(
        id=conversation.id, title=conversation.title,
        model_name=conversation.model_name,
        knowledge_enabled=conversation.knowledge_enabled,
        image_input_supported=image_input_support(conversation.model_name),
        updated_at=conversation.updated_at.replace(tzinfo=timezone.utc),
        generating=bool(conversation.busy_until and conversation.busy_until > utc_now()),
        messages=[message_info(message) for message in conversation.messages],
    )


async def list_user_conversations(db: AsyncSession, user_id: int) -> list[Conversation]:
    """按更新时间读取当前个人资料的会话和消息。"""
    rows = await db.scalars(select(Conversation).where(
        Conversation.user_id == user_id,
    ).options(selectinload(Conversation.messages).selectinload(Message.attachment_links)
              .selectinload(MessageAttachment.attachment)).order_by(
        Conversation.updated_at.desc(), Conversation.id,
    ))
    return list(rows)


async def create_user_conversation(db: AsyncSession, user_id: int, model_name: str,
                                   first_message: str, knowledge_enabled: bool = False) -> Conversation:
    """创建时保存首次问题前 28 个字符作为标题，不依赖模型返回。"""
    conversation = Conversation(id=str(uuid4()), user_id=user_id, model_name=model_name,
                                title=first_message[:28], knowledge_enabled=knowledge_enabled)
    db.add(conversation)
    await db.commit()
    logger.info("会话创建成功 会话=%s 用户=%s 模型=%s", conversation.id, user_id, model_name)
    return await get_user_conversation(db, user_id, conversation.id)


async def delete_user_conversation(db: AsyncSession, conversation_id: str) -> None:
    """删除会话，消息通过外键级联删除。"""
    await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await db.commit()
    logger.info("会话删除成功 会话=%s", conversation_id)


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
    logger.info("会话标题修改成功 会话=%s 用户=%s", conversation_id, user_id)
    return await get_user_conversation(db, user_id, conversation_id)


async def switch_conversation_model(db: AsyncSession, user_id: int, conversation_id: str,
                                    model_name: str) -> ConversationModelInfo:
    """短事务内切换模型；生成占用期间不允许修改，历史回答的模型记录保持原值。"""
    await db.commit()
    conversation = await owned_conversation(db, user_id, conversation_id, lock=True)
    if conversation.busy_until and conversation.busy_until > utc_now():
        raise HTTPException(409, "会话正在回答，请停止或等待完成后再切换模型")
    previous_model = conversation.model_name
    conversation.model_name = model_name
    await db.commit()
    logger.info("会话模型切换成功 会话=%s 原模型=%s 新模型=%s", conversation_id, previous_model, model_name)
    return ConversationModelInfo(id=conversation_id, model_name=model_name,
                                 image_input_supported=image_input_support(model_name))


async def switch_conversation_knowledge(db: AsyncSession, user_id: int, conversation_id: str,
                                        enabled: bool) -> ConversationKnowledgeInfo:
    """和生成领取使用同一把短事务行锁，本轮开始后不能改变开关。"""
    await db.commit()
    conversation = await owned_conversation(db, user_id, conversation_id, lock=True)
    if conversation.busy_until and conversation.busy_until > utc_now():
        raise HTTPException(409, "会话正在回答，请停止或等待完成后再切换知识库")
    conversation.knowledge_enabled = enabled
    await db.commit()
    logger.info("会话知识库开关已保存 会话=%s 开启=%s", conversation_id, enabled)
    return ConversationKnowledgeInfo(id=conversation_id, knowledge_enabled=enabled)


async def owned_conversation(db: AsyncSession, user_id: int, conversation_id: str, *, lock=False):
    """只读取属于当前个人档案的会话；没有匹配记录时返回 404。"""
    query = select(Conversation).where(
        Conversation.id == conversation_id, Conversation.user_id == user_id,
    ).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    else:
        query = query.options(selectinload(Conversation.messages).selectinload(Message.attachment_links)
                              .selectinload(MessageAttachment.attachment))
    conversation = await db.scalar(query)
    if not conversation:
        raise HTTPException(404, "会话不存在")
    return conversation


async def get_user_conversation(db: AsyncSession, user_id: int, conversation_id: str, *, lock=False):
    """对外提供带归属校验的会话查询。"""
    return await owned_conversation(db, user_id, conversation_id, lock=lock)


def model_history(messages: list[Message], image_count=0, image_size=0,
                  include_images=True) -> list[tuple[str, str | list[dict]]]:
    """按整轮保留最近历史，文字和图片都设上限；生产调用放在工作线程读文件。"""
    rounds = []
    total = 0
    messages = messages[-40:]
    for index in range(len(messages) - 2, -1, -2):
        pair = messages[index:index + 2]
        size = sum(len(message.content) for message in pair)
        images = [item for message in pair for item in message.attachments] if include_images else []
        pair_image_size = sum(item.size for item in images)
        if (total + size > 60000 or image_count + len(images) > MAX_IMAGES
                or image_size + pair_image_size > MAX_IMAGE_TOTAL_SIZE):
            break
        rounds.insert(0, [(message.role, message_content(message.content,
                          [attachment_info(item) for item in message.attachments] if include_images else [])) for message in pair])
        total += size
        image_count += len(images)
        image_size += pair_image_size
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
        logger.debug("释放本次生成占用 会话=%s", conversation_id)
    await db.commit()


@dataclass(slots=True)
class Generation:
    """一次生成的占用和上下文；跨事务传递普通数据，不持有数据库连接。"""
    user_id: int
    conversation_id: str
    body: SendMessageRequest
    duration: float
    lease: str | None = None
    model_name: str = ""
    history: list[tuple[str, str | list[dict]]] = field(default_factory=list)
    current_content: str | list[dict] | None = None
    sequence: int = 0
    saved: ConversationTurnInfo | None = None
    citations: list[dict] = field(default_factory=list)
    knowledge_enabled: bool = False


def turn_info(conversation: Conversation, request_id: str, messages: list[Message]) -> ConversationTurnInfo:
    """把本轮已保存问答转成流式完成事件的数据。"""
    return ConversationTurnInfo(
        request_id=request_id, conversation_id=conversation.id,
        updated_at=conversation.updated_at.replace(tzinfo=timezone.utc),
        messages=[message_info(message) for message in messages],
    )


async def prepare_generation(db: AsyncSession, user_id: int, conversation_id: str,
                             body: SendMessageRequest, timeout: float) -> Generation:
    """检查会话和重复请求，标记本次回答正在进行，并读取历史消息。

    同一会话同时只允许生成一轮新回答；已完成的重复请求直接返回保存结果。
    数据库行锁只用于这段准备过程，提交事务后释放，不一直锁到模型回答结束。
    """
    started = perf_counter()
    generation = Generation(user_id, conversation_id, body, max(10, timeout * 3))
    request_id = str(body.request_id)
    # 依赖查询可能已经开启事务，先结束它，再用行锁读取最新状态。
    await db.commit()
    conversation = await owned_conversation(db, user_id, conversation_id, lock=True)
    generation.model_name = conversation.model_name
    generation.knowledge_enabled = conversation.knowledge_enabled
    previous = list((await db.scalars(select(Message).where(
        Message.conversation_id == conversation_id, Message.request_id == request_id,
    ).options(selectinload(Message.attachment_links).selectinload(MessageAttachment.attachment))
        .order_by(Message.sequence))).all())
    if previous:
        if (previous[0].content != body.message or
                [item.id for item in previous[0].attachments] != [str(item) for item in body.attachment_ids]):
            logger.warning("消息编号被用于不同问题 会话=%s 消息请求=%s", conversation_id, request_id)
            raise HTTPException(409, "同一个请求编号不能用于不同的问题或图片")
        generation.saved = turn_info(conversation, request_id, previous)
        await db.commit()
        logger.info("重放已保存的问答 会话=%s 消息请求=%s", conversation_id, request_id)
        return generation
    if conversation.busy_until and conversation.busy_until > utc_now():
        logger.warning("会话正忙，拒绝重复生成 会话=%s 消息请求=%s", conversation_id, request_id)
        raise HTTPException(409, "这段会话正在回答，请稍候再试")
    supports_images = image_input_support(conversation.model_name)
    if body.attachment_ids and supports_images is False:
        logger.warning("拒绝向纯文本模型发送图片 会话=%s 模型=%s 图片数=%s",
                       conversation_id, conversation.model_name, len(body.attachment_ids))
        raise HTTPException(422, f"当前会话使用 {conversation.model_name}，仅支持文字；请切换到视觉模型后再发送图片")
    attachments = await get_attachments(db, user_id, [str(item) for item in body.attachment_ids])
    # 给本次执行生成一个内部编号，再把“谁在回答、最晚占用到何时”写入会话表。
    # 后续请求看到尚未到期的标记，就不能在同一会话里同时开始另一轮回答。
    # 到期时间用于服务异常退出、来不及清理标记时，允许后续请求恢复使用会话。
    generation.lease = str(uuid4())
    conversation.active_request = generation.lease
    conversation.busy_until = utc_now() + timedelta(seconds=generation.duration + 30)
    history_rows = list((await db.scalars(select(Message).where(
        Message.conversation_id == conversation_id,
    ).options(selectinload(Message.attachment_links).selectinload(MessageAttachment.attachment))
        .order_by(Message.sequence.desc()).limit(40))).all())[::-1]
    generation.sequence = history_rows[-1].sequence if history_rows else 0
    await db.commit()
    try:
        # 先释放行锁和连接，再读图和编码，避免持锁等待文件处理。
        generation.current_content = await anyio.to_thread.run_sync(
            message_content, body.message, [attachment_info(item) for item in attachments],
        )
        generation.history = await anyio.to_thread.run_sync(
            model_history, history_rows, len(attachments), sum(item.size for item in attachments),
            supports_images is not False,
        )
    except BaseException:
        # 领取占用后，即使图片丢失或客户端取消，也必须释放本次占用。
        with anyio.CancelScope(shield=True):
            await release_generation(db, user_id, conversation_id, generation.lease)
        raise
    logger.info("生成准备完成 会话=%s 消息请求=%s 模型=%s 历史消息数=%s 本轮图片数=%s 耗时=%.0fms", conversation_id,
                request_id, generation.model_name, len(generation.history), len(attachments), (perf_counter() - started) * 1000)
    return generation


async def save_generation(db: AsyncSession, generation: Generation, reply: str) -> ConversationTurnInfo:
    """核对本次占用后原子保存整轮问答，提交成功才返回完成结果。"""
    started = perf_counter()
    if not reply.strip():
        raise ModelError("模型返回了空回答，请重试")
    conversation = await owned_conversation(db, generation.user_id, generation.conversation_id, lock=True)
    if conversation.active_request != generation.lease:
        logger.warning("保存回答时生成占用已失效 会话=%s 消息请求=%s",
                       generation.conversation_id, generation.body.request_id)
        raise HTTPException(409, "本次请求已过期，请重新打开会话")
    await validate_citations(db, generation.user_id, generation.citations)
    # MySQL 当前字段精度为秒，首个响应与按请求编号重放时保持同一时间。
    conversation.updated_at = utc_now().replace(microsecond=0)
    conversation.active_request = None
    conversation.busy_until = None
    request_id = str(generation.body.request_id)
    attachments = await get_attachments(db, generation.user_id, [str(item) for item in generation.body.attachment_ids])
    messages = [
        Message(id=str(uuid4()), conversation_id=generation.conversation_id, sequence=generation.sequence + 1,
                request_id=request_id, role="user", content=generation.body.message,
                attachment_links=[MessageAttachment(position=index, attachment=item)
                                  for index, item in enumerate(attachments)]),
        Message(id=str(uuid4()), conversation_id=generation.conversation_id, sequence=generation.sequence + 2,
                request_id=request_id, role="assistant", content=reply, model=generation.model_name, attachment_links=[], citations=generation.citations),
    ]
    db.add_all(messages)
    result = turn_info(conversation, request_id, messages)
    await db.commit()
    logger.info("问答保存成功 会话=%s 消息请求=%s 回答字符数=%s 耗时=%.0fms", generation.conversation_id,
                request_id, len(reply), (perf_counter() - started) * 1000)
    return result


async def answer_message(db: AsyncSession, generation: Generation, agent):
    """普通 JSON 发送：共用生成逻辑，等待完整回答后返回会话。"""
    user_id, conversation_id, body = generation.user_id, generation.conversation_id, generation.body
    if not generation.saved:
        try:
            async with asyncio.timeout(generation.duration):
                result = await agent.ainvoke({"messages": [*generation.history,
                                                          ("user", generation.current_content or body.message)]})
            generation.citations = result.get("citations", [])
            await save_generation(db, generation, result["messages"][-1].text)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                logger.info("普通回答取消 会话=%s 消息请求=%s", conversation_id, body.request_id)
            elif isinstance(exc, (ModelError, TimeoutError)):
                logger.warning("普通回答失败 会话=%s 消息请求=%s 异常类型=%s",
                               conversation_id, body.request_id, type(exc).__name__)
            else:
                logger.error("普通回答发生异常 会话=%s 消息请求=%s",
                             conversation_id, body.request_id, exc_info=True)
            await release_generation(db, user_id, conversation_id, generation.lease)
            raise
    return conversation_info(await owned_conversation(db, user_id, conversation_id))
