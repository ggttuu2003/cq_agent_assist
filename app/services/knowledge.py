"""个人知识资料的管理与检索；接口不接触磁盘路径和向量服务细节。"""

import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import HTTPException, UploadFile
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.db.models import utc_now
from app.db.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeJob, KnowledgeVersion
from app.knowledge.adapters import MilvusIndex, TextEmbeddings
from app.knowledge.parsing import KnowledgeError, decode_text
from app.schemas.knowledge import Citation, KnowledgeDocumentInfo, KnowledgePage

logger = logging.getLogger(__name__)
KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge"
MAX_FILE_SIZE = 20 * 1024 * 1024
ACTIVE_STATUSES = {"queued", "parsing", "indexing"}


def save_original(version_id: str, data: bytes):
    """文件路径只由后端 UUID 生成，用户文件名仅用于显示。"""
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    (KNOWLEDGE_DIR / version_id).write_bytes(data)


async def owned_document(db, user_id: int, document_id: str, *, lock=False):
    """读取自己的资料，删除和更新使用短事务行锁。"""
    query = select(KnowledgeDocument).where(KnowledgeDocument.id == document_id, KnowledgeDocument.user_id == user_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    item = await db.scalar(query)
    if item is None:
        raise HTTPException(404, "资料不存在或已经删除")
    return item


async def document_info(db, document: KnowledgeDocument) -> KnowledgeDocumentInfo:
    """列表只读取轻量版本信息，不加载整份正文。"""
    version_id = document.target_version_id or document.active_version_id
    version = (await db.execute(select(KnowledgeVersion.size, KnowledgeVersion.chunk_count).where(
        KnowledgeVersion.id == version_id))).first()
    active_count = await db.scalar(select(KnowledgeVersion.chunk_count).where(
        KnowledgeVersion.id == document.active_version_id)) if document.active_version_id else 0
    job = await db.scalar(select(KnowledgeJob).where(KnowledgeJob.document_id == document.id,
        KnowledgeJob.kind == ("delete" if document.status in {"deleting", "cleanup_failed"} else "index"),
        *([] if document.status in {"deleting", "cleanup_failed"} else [KnowledgeJob.version_id == document.target_version_id]))
                          .order_by(KnowledgeJob.created_at.desc(), KnowledgeJob.id.desc()).limit(1))
    return KnowledgeDocumentInfo(id=document.id, name=document.name, source_type=document.source_type,
        status=document.status, error=document.error, active_version_id=document.active_version_id,
        target_version_id=document.target_version_id, size=version.size if version else 0,
        chunk_count=active_count or 0, completed=job.completed if job else 0, total=job.total if job else 0,
        job_id=job.id if job else None, updated_at=document.updated_at.replace(tzinfo=timezone.utc))


async def list_documents(db, user_id: int, query: str, status: str | None, cursor: str | None, limit: int):
    """通过时间和编号稳定分页，避免资料多时把全部内容加载到浏览器。"""
    statement = select(KnowledgeDocument).where(KnowledgeDocument.user_id == user_id)
    if query:
        statement = statement.where(KnowledgeDocument.name.contains(query, autoescape=True))
    if status:
        statement = statement.where(KnowledgeDocument.status == status)
    if cursor:
        try:
            stamp, identifier = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            stamp = datetime.fromisoformat(stamp)
            if not isinstance(identifier, str) or len(identifier) != 36:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HTTPException(422, "分页位置不正确，请重新加载") from None
        statement = statement.where(or_(KnowledgeDocument.updated_at < stamp,
            and_(KnowledgeDocument.updated_at == stamp, KnowledgeDocument.id < identifier)))
    rows = list(await db.scalars(statement.order_by(KnowledgeDocument.updated_at.desc(), KnowledgeDocument.id.desc()).limit(limit + 1)))
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = base64.urlsafe_b64encode(json.dumps([rows[-1].updated_at.isoformat(), rows[-1].id]).encode()).decode() if more else None
    return KnowledgePage(items=[await document_info(db, row) for row in rows], next_cursor=next_cursor)


async def upload_document(db, user_id: int, file: UploadFile, document_id: str | None = None):
    """保存原文件和持久化任务；重复文件返回原资料，显式更新才替换内容。"""
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    filename = "".join(char for char in filename if char.isprintable()).strip()[:200]
    suffix = Path(filename).suffix.lower()
    if suffix not in {".txt", ".md", ".markdown"}:
        raise HTTPException(415, "当前支持 TXT 和 Markdown，PDF / DOCX 将在后续接入")
    data = await file.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, "单个文件不能超过 20 MB")
    # 先验证确实是文字，避免二进制文件伪装为 TXT 排入队列。
    try:
        await anyio.to_thread.run_sync(decode_text, data)
    except KnowledgeError as exc:
        raise HTTPException(415, str(exc)) from None
    fingerprint = hashlib.sha256(data).hexdigest()
    await db.commit()
    if document_id:
        document = await owned_document(db, user_id, document_id, lock=True)
        if document.status in ACTIVE_STATUSES or document.status in {"deleting", "cleanup_failed"}:
            raise HTTPException(409, "资料正在处理，请完成后再更新")
        if document.fingerprint == fingerprint and document.active_version_id:
            return await document_info(db, document)
    else:
        existing = await db.scalar(select(KnowledgeDocument).where(
            KnowledgeDocument.user_id == user_id, KnowledgeDocument.fingerprint == fingerprint))
        if existing:
            return await document_info(db, existing)
        document = KnowledgeDocument(id=str(uuid4()), user_id=user_id, name=filename, fingerprint=fingerprint)
        db.add(document)
    version_id = str(uuid4())
    try:
        await anyio.to_thread.run_sync(save_original, version_id, data)
        document.fingerprint = fingerprint
        document.target_version_id = version_id
        document.status, document.error = "queued", None
        document.updated_at = utc_now()
        await db.flush()
        db.add(KnowledgeVersion(id=version_id, document_id=document.id, filename=filename, size=len(data),
            content_hash=fingerprint, media_type="text/markdown" if suffix != ".txt" else "text/plain"))
        db.add(KnowledgeJob(id=str(uuid4()), document_id=document.id, version_id=version_id))
        await db.commit()
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await db.rollback()
            await anyio.to_thread.run_sync((KNOWLEDGE_DIR / version_id).unlink, True)
        if isinstance(exc, IntegrityError):
            existing = await db.scalar(select(KnowledgeDocument).where(
                KnowledgeDocument.user_id == user_id, KnowledgeDocument.fingerprint == fingerprint))
            if existing and not document_id:
                return await document_info(db, existing)
            raise HTTPException(409, "这份内容已在个人知识库中，请查看已有资料") from None
        raise
    logger.info("知识资料提交成功 资料=%s 版本=%s 字节数=%s", document.id, version_id, len(data))
    return await document_info(db, document)


async def reprocess_document(db, user_id: int, document_id: str):
    """重试或重建均创建独立目标版本，旧版本继续可用。"""
    await db.commit()
    document = await owned_document(db, user_id, document_id, lock=True)
    if document.status in ACTIVE_STATUSES or document.status in {"deleting", "cleanup_failed"}:
        raise HTTPException(409, "资料正在处理，请稍候")
    old = await db.get(KnowledgeVersion, document.target_version_id or document.active_version_id)
    if old is None:
        raise HTTPException(409, "原资料不存在，请重新上传")
    version_id = str(uuid4())
    try:
        data = await anyio.to_thread.run_sync((KNOWLEDGE_DIR / old.id).read_bytes)
        await anyio.to_thread.run_sync(save_original, version_id, data)
        db.add(KnowledgeVersion(id=version_id, document_id=document.id, filename=old.filename, size=old.size,
            media_type=old.media_type, content_hash=old.content_hash))
        db.add(KnowledgeJob(id=str(uuid4()), document_id=document.id, version_id=version_id))
        document.target_version_id, document.status, document.error = version_id, "queued", None
        await db.commit()
    except BaseException as exc:
        with anyio.CancelScope(shield=True):
            await db.rollback()
            await anyio.to_thread.run_sync((KNOWLEDGE_DIR / version_id).unlink, True)
        if isinstance(exc, OSError):
            raise HTTPException(409, "原文件已经丢失，请重新上传") from None
        raise
    return await document_info(db, document)


async def delete_document(db, user_id: int, document_id: str):
    """先停止参与检索，再提交删除任务；迟到的处理任务不能重新发布。"""
    await db.commit()
    document = await owned_document(db, user_id, document_id, lock=True)
    if document.status != "deleting":
        document.status, document.error = "deleting", None
        db.add(KnowledgeJob(id=str(uuid4()), document_id=document.id, kind="delete"))
        await db.commit()
    return await document_info(db, document)


async def validate_citations(db, user_id: int, citations: list[dict]):
    """保存回答前锁住引用资料并核对归属，删除先完成时拒绝保存本轮。"""
    if not citations:
        return
    identifiers = sorted({item["document_id"] for item in citations})
    rows = list(await db.scalars(select(KnowledgeDocument).where(
        KnowledgeDocument.id.in_(identifiers), KnowledgeDocument.user_id == user_id)
        .order_by(KnowledgeDocument.id).with_for_update()))
    if len(rows) != len(identifiers) or any(row.status in {"deleting", "cleanup_failed"} for row in rows):
        raise HTTPException(409, "回答引用的资料已被删除，请重新提问")


async def rename_document(db, user_id: int, document_id: str, name: str):
    """只修改显示名和更新时间，不影响已发布的向量版本。"""
    item = await owned_document(db, user_id, document_id, lock=True)
    if item.status in {"deleting", "cleanup_failed"}:
        raise HTTPException(409, "资料正在删除")
    item.name, item.updated_at = name, utc_now()
    await db.commit()
    return await document_info(db, item)


async def document_content(db, user_id: int, document_id: str, version_id: str | None, offset: int, limit: int):
    """分页读取指定版本正文，旧版本失效时明确提示而不替换成新正文。"""
    item = await owned_document(db, user_id, document_id)
    if item.status in {"deleting", "cleanup_failed"}:
        raise HTTPException(404, "资料已删除，历史回答中仅保留引用摘录")
    version = await db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.document_id == item.id,
        KnowledgeVersion.id == (version_id or item.active_version_id or item.target_version_id)))
    if version is None:
        raise HTTPException(404, "资料版本不存在")
    if version.content is None:
        raise HTTPException(409, "这个版本正在解析或原文已清理，请查看处理状态或历史引用摘录")
    return {"document_id": item.id, "version_id": version.id, "name": item.name,
        "is_current": version.id == item.active_version_id, "offset": offset,
        "content": version.content[offset:offset + limit], "total": len(version.content),
        "next_offset": offset + limit if offset + limit < len(version.content) else None}


async def document_chunks(db, user_id: int, document_id: str, version_id: str | None,
                          offset: int, limit: int, chunk_id: str | None = None):
    """按顺序分页；从引用进入时定位到目标片段所在页。"""
    item = await owned_document(db, user_id, document_id)
    if item.status in {"deleting", "cleanup_failed"}:
        raise HTTPException(404, "资料已经删除")
    version = await db.scalar(select(KnowledgeVersion).where(KnowledgeVersion.document_id == item.id,
        KnowledgeVersion.id == (version_id or item.active_version_id or item.target_version_id)))
    if not version:
        raise HTTPException(404, "资料版本不存在")
    # 引用可能位于第几十段，直接读取包含目标片段的那一页。
    if chunk_id:
        position = await db.scalar(select(KnowledgeChunk.position).where(
            KnowledgeChunk.id == chunk_id, KnowledgeChunk.version_id == version.id))
        if position is None:
            raise HTTPException(409, "引用原文已经清理，请查看历史引用摘录")
        offset = position // limit * limit
    rows = list(await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.version_id == version.id)
                                 .order_by(KnowledgeChunk.position).offset(offset).limit(limit + 1)))
    return {"items": [{"id": row.id, "location": row.location, "start": row.start, "end": row.end,
        "content": row.content} for row in rows[:limit]], "next_offset": offset + limit if len(rows) > limit else None}


async def original_file(db, user_id: int, document_id: str):
    """验证归属后返回可下载的原文件信息，磁盘路径不接受外部输入。"""
    item = await owned_document(db, user_id, document_id)
    if item.status in {"deleting", "cleanup_failed"}:
        raise HTTPException(404, "资料已经删除")
    version = await db.get(KnowledgeVersion, item.active_version_id or item.target_version_id)
    if not version:
        raise HTTPException(404, "原文件不存在")
    path = KNOWLEDGE_DIR / version.id
    if not await anyio.to_thread.run_sync(path.is_file):
        raise HTTPException(404, "原文件已经清理或丢失")
    return path, version.filename, version.media_type


async def job_info(db, user_id: int, job_id: str):
    """任务状态同样通过资料归属校验。"""
    job = await db.get(KnowledgeJob, job_id)
    if not job:
        raise HTTPException(404, "任务不存在或资料已删除")
    await owned_document(db, user_id, job.document_id)
    return {"id": job.id, "status": job.status, "stage": job.stage, "completed": job.completed,
        "total": job.total, "error": job.error, "attempts": job.attempts,
        "created_at": job.created_at.replace(tzinfo=timezone.utc)}


class KnowledgeService:
    """供 API、处理任务和 Agent 共用的服务，网络对象复用，数据库连接按需创建。"""

    def __init__(self, settings, sessions, client, *, embeddings=None, index=None):
        self.settings, self.sessions = settings, sessions
        self.embeddings = embeddings or TextEmbeddings(settings, client)
        self.index = index or MilvusIndex(settings)

    @property
    def configuration_error(self):
        """仅检查本地必要配置，不在页面首次打开时调用付费接口。"""
        return self.embeddings.error

    async def summary(self, user_id: int):
        """返回数量与当前配置提示，聊天工具无需加载全部文件名和正文。"""
        async with self.sessions() as db:
            rows = (await db.execute(select(KnowledgeDocument.status, func.count()).where(
                KnowledgeDocument.user_id == user_id).group_by(KnowledgeDocument.status))).all()
            ready = await db.scalar(select(func.count()).select_from(KnowledgeDocument).where(
                KnowledgeDocument.user_id == user_id, KnowledgeDocument.active_version_id.is_not(None),
                KnowledgeDocument.status.not_in(["deleting", "cleanup_failed"])))
        return {"total": sum(count for _, count in rows), "ready": ready or 0,
            "processing": sum(count for status, count in rows if status in ACTIVE_STATUSES),
            "configuration_error": self.configuration_error, "supported_formats": [".txt", ".md", ".markdown"],
            "max_file_size": MAX_FILE_SIZE, "max_batch_files": 10}

    async def search(self, user_id: int, query: str) -> list[dict]:
        """只搜索当前用户已生效版本，召回后再次查库过滤失效或跨用户结果。"""
        async with self.sessions() as db:
            versions = list(await db.scalars(select(KnowledgeVersion.id).join(KnowledgeDocument,
                KnowledgeDocument.active_version_id == KnowledgeVersion.id).where(
                KnowledgeDocument.user_id == user_id,
                KnowledgeDocument.status.not_in(["deleting", "cleanup_failed"]),
                KnowledgeVersion.profile == self.embeddings.profile)))
            total_ready = await db.scalar(select(func.count()).select_from(KnowledgeDocument).where(
                KnowledgeDocument.user_id == user_id, KnowledgeDocument.active_version_id.is_not(None),
                KnowledgeDocument.status.not_in(["deleting", "cleanup_failed"])))
        if not versions:
            if total_ready:
                raise KnowledgeError("Embedding 配置已变化，请在知识库中重新处理资料以建立兼容索引")
            return []
        if self.configuration_error:
            raise KnowledgeError(self.configuration_error)
        vector = (await self.embeddings.embed([query]))[0]
        hits = await self.index.search(self.embeddings.collection, user_id, versions, vector,
                                       min(50, self.settings.knowledge_top_k * 4))
        identifiers = [str(hit["id"]) for hit in hits if float(hit["distance"]) >= self.settings.knowledge_min_score]
        if not identifiers:
            return []
        async with self.sessions() as db:
            rows = (await db.execute(select(KnowledgeChunk, KnowledgeDocument).join(KnowledgeVersion,
                KnowledgeChunk.version_id == KnowledgeVersion.id).join(KnowledgeDocument,
                KnowledgeVersion.document_id == KnowledgeDocument.id).where(
                KnowledgeChunk.id.in_(identifiers), KnowledgeDocument.user_id == user_id,
                KnowledgeDocument.active_version_id == KnowledgeChunk.version_id,
                KnowledgeDocument.status.not_in(["deleting", "cleanup_failed"]),
                KnowledgeVersion.profile == self.embeddings.profile))).all()
        by_id, results, seen = {chunk.id: (chunk, doc) for chunk, doc in rows}, [], set()
        for identifier in identifiers:
            if identifier not in by_id:
                continue
            chunk, doc = by_id[identifier]
            fingerprint = hashlib.sha256(chunk.content.strip().encode()).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            results.append(Citation(number=len(results) + 1, document_id=doc.id, version_id=chunk.version_id,
                chunk_id=chunk.id, title=doc.name, location=chunk.location, quote=chunk.content[:1200]).model_dump())
            if len(results) >= self.settings.knowledge_top_k:
                break
        logger.info("知识检索完成 用户=%s 候选数=%s 命中数=%s", user_id, len(hits), len(results))
        return results

    async def close(self):
        """关闭向量库连接。"""
        await self.index.close()
