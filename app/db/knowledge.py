"""知识资料、内容版本、分段和持久化处理任务。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base, utc_now


class KnowledgeDocument(Base):
    """一份资料的显示信息；有效版本和正在处理的版本分别记录。"""
    __tablename__ = "cq_knowledge_document"
    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint", name="uq_knowledge_user_fingerprint"),
        Index("ix_knowledge_user_updated", "user_id", "updated_at"),
        {**Base.__table_args__, "comment": "个人知识资料"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="资料UUID")
    user_id: Mapped[int] = mapped_column(ForeignKey("cq_user.id", ondelete="CASCADE"), comment="所属个人资料")
    name: Mapped[str] = mapped_column(String(200), comment="显示名称")
    source_type: Mapped[str] = mapped_column(String(30), default="file", comment="来源类型")
    fingerprint: Mapped[str] = mapped_column(String(64), comment="最新提交内容的SHA256，用于去重")
    active_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="已完整生效的版本")
    target_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="最近提交处理的版本")
    status: Mapped[str] = mapped_column(String(30), default="queued", comment="最新处理状态")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="最近失败原因")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, comment="创建时间UTC")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, comment="最近修改时间UTC")


class KnowledgeVersion(Base):
    """原文件、解析正文和向量配置绑定在同一版本，重建不覆盖已生效内容。"""
    __tablename__ = "cq_knowledge_version"
    __table_args__ = {**Base.__table_args__, "comment": "知识资料内容版本"}
    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="版本UUID，也是本地原文件编号")
    document_id: Mapped[str] = mapped_column(ForeignKey("cq_knowledge_document.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(200), comment="原文件名")
    size: Mapped[int] = mapped_column(Integer, comment="原文件字节数")
    media_type: Mapped[str] = mapped_column(String(60), comment="原文件媒体类型")
    content_hash: Mapped[str] = mapped_column(String(64), comment="原始内容指纹")
    content: Mapped[str | None] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), nullable=True, comment="提取正文")
    profile: Mapped[str] = mapped_column(String(64), default="", comment="Embedding及分段配置指纹")
    collection: Mapped[str] = mapped_column(String(200), default="", comment="实际向量集合，清理时沿用")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, comment="本版本片段数量")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class KnowledgeChunk(Base):
    """可定位的文本片段，Milvus 只保存对应编号和向量。"""
    __tablename__ = "cq_knowledge_chunk"
    __table_args__ = (
        UniqueConstraint("version_id", "position", name="uq_knowledge_version_position"),
        {**Base.__table_args__, "comment": "知识资料分段"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("cq_knowledge_version.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, comment="片段顺序")
    start: Mapped[int] = mapped_column(Integer, comment="原文起始字符位置")
    end: Mapped[int] = mapped_column(Integer, comment="原文结束字符位置")
    location: Mapped[str] = mapped_column(String(500), comment="章节与段落位置")
    content: Mapped[str] = mapped_column(Text(), comment="片段正文")


class KnowledgeJob(Base):
    """MySQL 中持久保存的任务，执行器领取后用租约和心跳证明仍在工作。"""
    __tablename__ = "cq_knowledge_job"
    __table_args__ = (
        Index("ix_knowledge_job_claim", "status", "lease_until"),
        {**Base.__table_args__, "comment": "知识资料处理任务"},
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("cq_knowledge_document.id", ondelete="CASCADE"), index=True)
    version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="index", comment="建立索引或删除")
    status: Mapped[str] = mapped_column(String(20), default="queued", comment="待处理、运行中、成功或失败")
    stage: Mapped[str] = mapped_column(String(30), default="queued", comment="当前处理阶段")
    completed: Mapped[int] = mapped_column(Integer, default=0, comment="已处理片段数")
    total: Mapped[int] = mapped_column(Integer, default=0, comment="总片段数")
    attempts: Mapped[int] = mapped_column(Integer, default=0, comment="已领取次数")
    lease: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="本次执行占用UUID")
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="占用到期时间UTC")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
