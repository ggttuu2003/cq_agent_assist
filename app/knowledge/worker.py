"""领取 MySQL 持久化任务，解析和建索引；可随应用运行，也可独立运行。"""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4, uuid5, NAMESPACE_URL

import anyio
from sqlalchemy import delete, or_, select, update

from app.db.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeJob, KnowledgeVersion
from app.db.models import utc_now
from app.knowledge.parsing import KnowledgeError, decode_text, split_text
from app.services import knowledge as storage

logger = logging.getLogger(__name__)


class StaleWork(Exception):
    """任务租约失效、资料被删除或目标已变，当前执行不得再发布结果。"""


@dataclass(slots=True)
class Work:
    """跨事务传递任务编号，不把 ORM 会话或连接带到模型等待期间。"""
    id: str
    document_id: str
    version_id: str | None
    kind: str
    lease: str


class KnowledgeWorker:
    """每个执行器串行处理任务，多个执行器通过数据库行锁分配工作。"""

    def __init__(self, service):
        self.service, self.sessions = service, service.sessions

    async def claim(self):
        """领取待处理或租约已过期任务，SKIP LOCKED 避免多个进程重复领取。"""
        async with self.sessions() as db:
            jobs = list(await db.scalars(select(KnowledgeJob).where(or_(
                KnowledgeJob.status == "queued",
                (KnowledgeJob.status == "running") & (KnowledgeJob.lease_until < utc_now()),
            )).order_by(KnowledgeJob.created_at, KnowledgeJob.id).limit(20).with_for_update(skip_locked=True)))
            for job in jobs:
                document = await db.get(KnowledgeDocument, job.document_id)
                if document is None:
                    continue
                if document.status in {"deleting", "cleanup_failed"} and job.kind != "delete":
                    job.status = "cancelled"
                    continue
                if job.kind == "delete":
                    running = await db.scalar(select(KnowledgeJob.id).where(
                        KnowledgeJob.document_id == job.document_id, KnowledgeJob.id != job.id,
                        KnowledgeJob.status == "running", KnowledgeJob.lease_until > utc_now()).limit(1))
                    if running:
                        continue
                job.status, job.lease = "running", str(uuid4())
                job.lease_until = utc_now() + timedelta(seconds=90)
                job.attempts += 1
                job.error = None
                work = Work(job.id, job.document_id, job.version_id, job.kind, job.lease)
                await db.commit()
                return work
            await db.commit()
        return None

    async def heartbeat(self, work: Work):
        """模型等待期间独立续租，持有连接的时间仅为一条 UPDATE。"""
        while True:
            await asyncio.sleep(15)
            async with self.sessions() as db:
                await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == work.id,
                    KnowledgeJob.lease == work.lease, KnowledgeJob.status == "running").values(
                        lease_until=utc_now() + timedelta(seconds=90)))
                await db.commit()

    async def locked(self, db, work: Work):
        """所有写入都再次核对租约及资料目标，迟到的旧执行器不会覆盖新任务。"""
        document = await db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == work.document_id)
                                   .with_for_update().execution_options(populate_existing=True))
        job = await db.scalar(select(KnowledgeJob).where(KnowledgeJob.id == work.id).with_for_update())
        if not document or not job or job.lease != work.lease or job.status != "running" or job.lease_until < utc_now():
            raise StaleWork()
        if work.kind == "index" and (document.target_version_id != work.version_id or
                                      document.status in {"deleting", "cleanup_failed"}):
            raise StaleWork()
        return document, job

    async def index_document(self, work: Work):
        """先持久化正文和稳定片段编号，再分批写向量，全部完成才发布版本。"""
        service = self.service
        if service.configuration_error:
            raise KnowledgeError(service.configuration_error)
        async with self.sessions() as db:
            document, job = await self.locked(db, work)
            user_id = document.user_id
            document.status, job.stage = "parsing", "parsing"
            version = await db.get(KnowledgeVersion, work.version_id)
            # 重启时配置可能已经变化。保留旧集合记录，让重建后的清理任务仍能找到它。
            if version.profile and version.profile != service.embeddings.profile:
                raise KnowledgeError("Embedding 或分段配置已变化，请重新处理资料以建立新版本")
            # 记录集合后才开始远程写入，进程中断后清理任务仍知道数据写到了哪里。
            version.profile, version.collection = service.embeddings.profile, service.embeddings.collection
            await db.commit()
        try:
            data = await anyio.to_thread.run_sync((storage.KNOWLEDGE_DIR / work.version_id).read_bytes)
        except OSError:
            raise KnowledgeError("原文件已经丢失，请重新上传") from None
        content = await anyio.to_thread.run_sync(decode_text, data)
        pieces = await anyio.to_thread.run_sync(split_text, content, service.settings.knowledge_chunk_size,
                                                service.settings.knowledge_chunk_overlap)
        if not pieces:
            raise KnowledgeError("没有可建立索引的有效片段")
        # 同一版本和位置生成相同 UUID，恢复或重试不会创建重复向量。
        rows = [{"id": str(uuid5(NAMESPACE_URL, f"{work.version_id}:{index}")), "user_id": user_id,
                 "document_id": work.document_id, "version_id": work.version_id} for index in range(len(pieces))]
        async with self.sessions() as db:
            document, job = await self.locked(db, work)
            version = await db.get(KnowledgeVersion, work.version_id)
            version.content, version.chunk_count = content, len(pieces)
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.version_id == work.version_id))
            db.add_all([KnowledgeChunk(id=row["id"], version_id=work.version_id, position=index,
                start=piece.start, end=piece.end, location=piece.location, content=piece.content)
                for index, (row, piece) in enumerate(zip(rows, pieces))])
            document.status, job.stage, job.total, job.completed = "indexing", "indexing", len(pieces), 0
            await db.commit()
        await service.index.ensure(service.embeddings.collection, service.embeddings.dimensions)
        for start in range(0, len(pieces), 16):
            # 每批调用前核对状态，删除后尽快停止，不继续消耗模型额度。
            async with self.sessions() as db:
                await self.locked(db, work)
                await db.commit()
            vectors = await service.embeddings.embed([piece.content for piece in pieces[start:start + 16]])
            batch = [{**row, "vector": vector} for row, vector in zip(rows[start:start + 16], vectors)]
            await service.index.upsert(service.embeddings.collection, batch)
            async with self.sessions() as db:
                _, job = await self.locked(db, work)
                job.completed = start + len(batch)
                await db.commit()
        async with self.sessions() as db:
            document, job = await self.locked(db, work)
            document.active_version_id, document.status, document.error = work.version_id, "ready", None
            document.updated_at = utc_now()
            job.status, job.stage, job.finished_at, job.lease_until = "completed", "completed", utc_now(), None
            old_versions = list(await db.scalars(select(KnowledgeVersion.id).where(
                KnowledgeVersion.document_id == work.document_id, KnowledgeVersion.id != work.version_id)))
            for version_id in old_versions:
                previous = await db.scalar(select(KnowledgeJob.id).where(KnowledgeJob.version_id == version_id,
                    KnowledgeJob.kind == "cleanup", KnowledgeJob.status.in_(["queued", "running"])).limit(1))
                if not previous:
                    db.add(KnowledgeJob(id=str(uuid4()), document_id=work.document_id,
                        version_id=version_id, kind="cleanup"))
            await db.commit()
        logger.info("知识资料索引完成 任务=%s 资料=%s 片段数=%s", work.id, work.document_id, len(pieces))

    async def cleanup(self, work: Work):
        """先清理向量和文件再删库记录；任务未成功时记录保留，之后仍可继续清理。"""
        async with self.sessions() as db:
            document, job = await self.locked(db, work)
            if work.kind == "delete":
                versions = list(await db.scalars(select(KnowledgeVersion).where(
                    KnowledgeVersion.document_id == work.document_id)))
            else:
                if work.version_id in {document.active_version_id, document.target_version_id}:
                    raise StaleWork()
                version = await db.get(KnowledgeVersion, work.version_id)
                versions = [version] if version else []
            pairs = [(version.id, version.collection) for version in versions]
            job.stage = "cleanup"
            await db.commit()
        for version_id, collection in pairs:
            await self.service.index.delete_version(collection, version_id)
            await anyio.to_thread.run_sync((storage.KNOWLEDGE_DIR / version_id).unlink, True)
        async with self.sessions() as db:
            document, job = await self.locked(db, work)
            if work.kind == "delete":
                await db.delete(document)
            else:
                for version_id, _ in pairs:
                    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.version_id == version_id))
                    version = await db.get(KnowledgeVersion, version_id)
                    version.content = None
                job.status, job.stage, job.finished_at, job.lease_until = "completed", "completed", utc_now(), None
            await db.commit()

    async def fail(self, work: Work, error: str, stale=False):
        """只结束自己领取的任务，已被其他执行器接手的任务不受影响。"""
        async with self.sessions() as db:
            document = await db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.id == work.document_id).with_for_update())
            job = await db.scalar(select(KnowledgeJob).where(KnowledgeJob.id == work.id).with_for_update())
            if not job or job.lease != work.lease or job.status != "running":
                return
            job.status, job.error, job.finished_at, job.lease_until = "cancelled" if stale else "failed", error, utc_now(), None
            if document and not stale:
                if work.kind == "index" and document.target_version_id == work.version_id and document.status != "deleting":
                    document.status = "update_failed" if document.active_version_id else "failed"
                    document.error = error
                elif work.kind == "delete":
                    document.status, document.error = "cleanup_failed", error
                elif work.kind == "cleanup":
                    document.error = "资料可检索，但旧版本清理失败；重新处理时会再次尝试清理"
            await db.commit()

    async def run_once(self):
        """执行一项任务，供常驻循环和测试共用。"""
        work = await self.claim()
        if work is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(work))
        logger.info("知识任务开始 任务=%s 类型=%s 资料=%s", work.id, work.kind, work.document_id)
        try:
            async with asyncio.timeout(600):
                if work.kind == "index":
                    await self.index_document(work)
                else:
                    await self.cleanup(work)
        except StaleWork:
            await self.fail(work, "资料已删除或任务已过期", stale=True)
        except KnowledgeError as exc:
            await self.fail(work, str(exc))
            logger.warning("知识任务失败 任务=%s 原因=%s", work.id, exc)
        except asyncio.CancelledError:
            # 应用正常关闭时立即结束租约，下一次启动即可恢复，而不必等待90秒。
            with anyio.CancelScope(shield=True):
                async with self.sessions() as db:
                    await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == work.id,
                        KnowledgeJob.lease == work.lease, KnowledgeJob.status == "running").values(lease_until=utc_now()))
                    await db.commit()
            raise
        except Exception:
            logger.error("知识任务异常 任务=%s", work.id, exc_info=True)
            await self.fail(work, "资料处理超时或发生异常，请重试并查看后端日志")
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def run(self):
        """应用运行期间持续领取任务，数据库短暂不可用时稍后继续。"""
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception:
                logger.error("领取知识任务失败，将稍后重试", exc_info=True)
            await asyncio.sleep(2)
