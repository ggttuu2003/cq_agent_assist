"""记录真实模型请求，使用短事务与上下文隔离，不持有聊天请求的连接。"""

import base64
import hashlib
import json
import logging
from contextvars import ContextVar
from datetime import timedelta, timezone
from time import perf_counter
from uuid import uuid4

import anyio
import httpx
from fastapi import HTTPException
from sqlalchemy import select, update

from app.core.logging import request_id_context
from app.db.models import Attachment, Conversation, utc_now
from app.db.traces import AICall, AITrace
from app.schemas.traces import ModelCallInfo, RequestTraceInfo, TraceAttemptInfo
from app.services import attachments as attachment_storage

logger = logging.getLogger(__name__)
# 对象仅属于一次执行；ContextVar 会随异步子任务传递，不会改动共享模型客户端。
trace_context: ContextVar["TraceRun | None"] = ContextVar("ai_trace", default=None)
call_context: ContextVar["ModelCall | None"] = ContextVar("ai_model_call", default=None)


async def write_record(sessions, operation: str, row=None, statement=None) -> bool:
    """每次写入单独提交，失败只记安全警告；等待模型期间不占连接。"""
    try:
        # 外层停止生成时仍给清理机会，同时限制记录故障最多等待三秒。
        with anyio.CancelScope(shield=True), anyio.fail_after(3):
            async with sessions() as db:
                if row is not None:
                    db.add(row)
                if statement is not None:
                    await db.execute(statement)
                await db.commit()
        return True
    except Exception:
        logger.warning("调用记录保存失败 操作=%s，不影响正常问答", operation, exc_info=True)
        return False


class TraceRun:
    """绑定一轮生成的记录上下文，重放已保存结果时不创建新记录。"""

    def __init__(self, sessions, generation):
        self.sessions = sessions
        self.generation = generation
        self.id = str(uuid4())
        self.sequence = 0
        self.recorded = False
        self.outcome = None
        self.token = None

    async def __aenter__(self):
        """先保存执行信息，再让图开始；失败重试会保留此前的独立执行。"""
        if not self.generation.saved:
            self.recorded = await write_record(self.sessions, "开始执行", row=AITrace(
                id=self.id, conversation_id=self.generation.conversation_id,
                request_id=str(self.generation.body.request_id), http_request_id=request_id_context.get(),
                model=self.generation.model_name, knowledge_enabled=self.generation.knowledge_enabled,
                expires_at=utc_now() + timedelta(seconds=self.generation.duration + 30),
            ))
            self.token = trace_context.set(self)
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        """成功、失败、停止都收尾；恢复上下文，避免后续任务误用旧执行。"""
        try:
            if self.recorded:
                status = self.outcome or ("cancelled" if exc_type and not issubclass(exc_type, Exception)
                                          else "failed" if exc_type else "completed")
                await write_record(self.sessions, "结束执行", statement=update(AITrace).where(
                    AITrace.id == self.id).values(status=status, finished_at=utc_now()))
        finally:
            if self.token is not None:
                trace_context.reset(self.token)

    async def start_retrieval(self, query: str):
        """先留实际查询，用户在检索途中停止时也能看到已执行到哪里。"""
        if not self.recorded:
            return
        await write_record(self.sessions, "开始知识检索", statement=update(AITrace).where(
            AITrace.id == self.id).values(retrieval={"query": query, "elapsed_ms": None,
                "status": "running", "citations": [], "error": None, "message": "正在检索知识库"}))
        logger.info("知识检索查询 执行=%s 查询=%s", self.id, json.dumps(query, ensure_ascii=False))

    async def retrieval(self, query: str, result: dict, elapsed_ms: int):
        """保存最终回传片段；终端仅显示摘要，文件中的换行不打乱日志行。"""
        if not self.recorded:
            return
        snapshot = {"query": query, "elapsed_ms": elapsed_ms, "status": "error" if result.get("error") else "completed", **result}
        await write_record(self.sessions, "知识检索", statement=update(AITrace).where(
            AITrace.id == self.id).values(retrieval=snapshot))
        for item in result.get("citations", []):
            logger.info("知识检索片段 执行=%s 引用=%s 资料=%s 片段=%s 内容预览=%s", self.id,
                        item.get("number"), json.dumps(item.get("title"), ensure_ascii=False),
                        item.get("chunk_id"), json.dumps(item.get("quote", "")[:200], ensure_ascii=False))


class ModelCall:
    """保存一次节点调用的状态；请求体由发送前的 HTTP 钩子补齐。"""

    def __init__(self, run: TraceRun, model: str):
        self.run = run
        self.id = str(uuid4())
        run.sequence += 1
        self.sequence = run.sequence
        self.model = model
        self.started = perf_counter()
        self.recorded = False
        self.captured = False

    async def start(self):
        """先留下一次调用，序列化或连接失败时也有记录可查。"""
        if self.run.recorded:
            self.recorded = await write_record(self.run.sessions, "开始模型调用", row=AICall(
                id=self.id, trace_id=self.run.id, sequence=self.sequence, model=self.model,
            ))

    async def capture(self, body: dict):
        """按最终 HTTP JSON 保存，保留消息顺序和实际参数，不手动推算 SDK 默认值。"""
        if not self.recorded:
            return
        normalized, images, error = await anyio.to_thread.run_sync(
            normalize_request, body, self.run.generation.trace_images)
        self.captured = await write_record(self.run.sessions, "模型请求参数", statement=update(AICall).where(
            AICall.id == self.id).values(request_body=normalized, images=images, capture_error=error))
        logger.info("模型请求已记录 执行=%s 调用=%s 顺序=%s 成功=%s", self.run.id, self.id, self.sequence, self.captured)

    async def finish(self, outcome: str, error: BaseException | None):
        """保存安全错误类型和耗时，不把上游异常正文或鉴权信息写入记录。"""
        if not self.recorded:
            return
        values = dict(status=outcome, elapsed_ms=round((perf_counter() - self.started) * 1000),
                      finished_at=utc_now(), http_status=getattr(error, "status_code", None),
                      error=f"模型调用失败（{type(error).__name__}）" if error else None)
        if not self.captured:
            values["capture_error"] = "未捕获到完整模型请求，可能在发送前失败或记录写入失败"
        await write_record(self.run.sessions, "结束模型调用", statement=update(AICall).where(
            AICall.id == self.id).values(**values))


def normalize_request(body: dict, known_images: dict) -> tuple[dict, list[dict], str | None]:
    """图片变为可校验、可恢复的附件引用，其他业务内容原样保留。"""
    images, errors = {}, []

    def visit(value):
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            # 只处理真正的图片内容块，用户正文即使包含数据地址也不会被改写。
            image_url = result.get("image_url")
            if result.get("type") == "image_url" and isinstance(image_url, dict):
                url = image_url.get("url")
                if isinstance(url, str) and url.startswith("data:image/"):
                    digest = hashlib.sha256(url.encode()).hexdigest()
                    attachment = known_images.get(digest)
                    if attachment:
                        images[attachment["id"]] = {"attachment": attachment, "sha256": digest}
                        image_url["url"] = f"attachment://{attachment['id']}"
                    else:
                        errors.append("部分图片缺少附件映射，无法恢复完整请求")
                        image_url["url"] = "[未保存的图片内容]"
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    result = visit(body)
    # 兼容服务若额外带认证字段，只屏蔽顶层认证参数，不能改写工具 schema 或聊天内容。
    for key in result:
        if key.lower() in {"api_key", "authorization", "access_token"}:
            result[key] = "[已隐藏]"
    return result, list(images.values()), errors[0] if errors else None


async def capture_model_request(request: httpx.Request):
    """只在模型节点的调用上下文中捕获对话请求，不记录鉴权头、Embedding 或搜索。"""
    call = call_context.get()
    if call is None or not request.url.path.rstrip("/").endswith("/chat/completions"):
        return
    try:
        body = json.loads(await request.aread())
        await call.capture(body)
    except Exception:
        logger.warning("模型请求捕获失败，不影响模型发送", exc_info=True)


async def owned_trace_conversation(db, user_id: int, conversation_id: str):
    """校验会话归属，记录查询不能只凭可猜测的请求编号。"""
    exists = await db.scalar(select(Conversation.id).where(
        Conversation.id == conversation_id, Conversation.user_id == user_id))
    if exists is None:
        raise HTTPException(404, "会话不存在")


def timestamp(value):
    """数据库统一存UTC，接口显式带时区。"""
    return value.replace(tzinfo=timezone.utc) if value else None


async def get_request_trace(db, user_id: int, conversation_id: str, request_id: str) -> RequestTraceInfo:
    """一次加载执行和调用，老消息返回空列表，不复制正文到聊天列表接口。"""
    await owned_trace_conversation(db, user_id, conversation_id)
    attempts = list(await db.scalars(select(AITrace).where(AITrace.conversation_id == conversation_id,
        AITrace.request_id == request_id).order_by(AITrace.created_at, AITrace.id)))
    calls = list(await db.scalars(select(AICall).join(AITrace).where(AITrace.conversation_id == conversation_id,
        AITrace.request_id == request_id).order_by(AICall.sequence))) if attempts else []
    result = RequestTraceInfo(conversation_id=conversation_id, request_id=request_id)
    for attempt in attempts:
        interrupted = attempt.status == "running" and attempt.expires_at < utc_now()
        rows = []
        for call in calls:
            if call.trace_id != attempt.id:
                continue
            rows.append(ModelCallInfo(id=call.id, sequence=call.sequence, model=call.model,
                status="interrupted" if interrupted and call.status == "running" else call.status,
                request_body=call.request_body, images=call.images or [], capture_error=call.capture_error,
                http_status=call.http_status, error=call.error, elapsed_ms=call.elapsed_ms,
                created_at=timestamp(call.created_at), finished_at=timestamp(call.finished_at)))
        retrieval = attempt.retrieval
        if retrieval and retrieval.get("status") == "running" and (interrupted or attempt.status != "running"):
            retrieval = {**retrieval, "status": "interrupted" if interrupted else attempt.status,
                         "message": "检索未完成，本次执行已停止或中断"}
        result.attempts.append(TraceAttemptInfo(id=attempt.id, http_request_id=attempt.http_request_id,
            model=attempt.model, knowledge_enabled=attempt.knowledge_enabled,
            status="interrupted" if interrupted else attempt.status, retrieval=retrieval,
            created_at=timestamp(attempt.created_at), finished_at=timestamp(attempt.finished_at), calls=rows))
    return result


async def original_request(db, user_id: int, conversation_id: str, request_id: str, call_id: str) -> dict:
    """导出前校验会话、调用和图片归属，缺少原图时拒绝冒充完整请求。"""
    await owned_trace_conversation(db, user_id, conversation_id)
    call = await db.scalar(select(AICall).join(AITrace).where(AITrace.conversation_id == conversation_id,
        AITrace.request_id == request_id, AICall.id == call_id))
    if call is None:
        raise HTTPException(404, "模型调用不存在")
    if call.request_body is None or call.capture_error:
        raise HTTPException(409, call.capture_error or "尚未捕获模型请求")
    body, images = call.request_body, call.images or []
    identifiers = [item["attachment"]["id"] for item in images]
    owned = set(await db.scalars(select(Attachment.id).where(
        Attachment.user_id == user_id, Attachment.id.in_(identifiers)))) if identifiers else set()
    if owned != set(identifiers):
        raise HTTPException(409, "原始图片附件已不可用，无法恢复完整请求")
    # 释放查询事务再读图片，不在文件处理期间占用数据库连接。
    await db.commit()
    return await anyio.to_thread.run_sync(restore_images, body, images)


def restore_images(body: dict, images: list[dict]) -> dict:
    """恢复实际发送的数据地址，并用摘要拒绝已被替换的文件。"""
    replacements = {}
    for image in images:
        attachment = image["attachment"]
        try:
            encoded = base64.b64encode((attachment_storage.ATTACHMENT_DIR / attachment["id"]).read_bytes()).decode("ascii")
        except OSError:
            raise HTTPException(409, "原始图片文件已缺失，无法恢复完整请求") from None
        url = f"data:{attachment['media_type']};base64,{encoded}"
        if hashlib.sha256(url.encode()).hexdigest() != image["sha256"]:
            raise HTTPException(409, "原始图片文件已变化，无法恢复完整请求")
        replacements[f"attachment://{attachment['id']}"] = url

    def visit(value):
        if isinstance(value, dict):
            result = {key: visit(item) for key, item in value.items()}
            image_url = result.get("image_url")
            if result.get("type") == "image_url" and isinstance(image_url, dict):
                url = image_url.get("url")
                if isinstance(url, str) and url in replacements:
                    image_url["url"] = replacements[url]
            return result
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value

    return visit(body)
