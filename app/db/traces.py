"""会话执行和模型调用记录，不依赖成功保存的消息。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.mysql import DATETIME

from app.db.models import Base, utc_now

# 同一秒内重试也能按实际开始顺序排列。
TRACE_TIME = DateTime().with_variant(DATETIME(fsp=6), "mysql")


class AITrace(Base):
    """一次实际执行；重试复用消息请求编号，但生成新的执行编号。"""
    __tablename__ = "cq_ai_trace"
    __table_args__ = (
        Index("ix_cq_ai_trace_request", "conversation_id", "request_id", "created_at"),
        {**Base.__table_args__, "comment": "问答执行记录"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="本次执行UUID")
    conversation_id: Mapped[str] = mapped_column(ForeignKey("cq_conversation.id", ondelete="CASCADE"), comment="所属会话")
    request_id: Mapped[str] = mapped_column(String(36), comment="客户端消息请求编号，失败重试可复用")
    http_request_id: Mapped[str] = mapped_column(String(36), comment="终端日志中的HTTP请求编号")
    model: Mapped[str] = mapped_column(String(200), comment="本次执行使用的模型")
    knowledge_enabled: Mapped[bool] = mapped_column(Boolean, comment="本次执行的知识库开关")
    status: Mapped[str] = mapped_column(String(20), default="running", comment="running/completed/failed/cancelled")
    retrieval: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="查询、实际参考片段和检索结果快照")
    created_at: Mapped[datetime] = mapped_column(TRACE_TIME, default=utc_now, comment="开始时间UTC")
    expires_at: Mapped[datetime] = mapped_column(TRACE_TIME, comment="预计最晚结束时间，用于识别进程中断")
    finished_at: Mapped[datetime | None] = mapped_column(TRACE_TIME, nullable=True, comment="结束时间UTC")


class AICall(Base):
    """一次模型调用，保存SDK最终请求体，图片使用可恢复的附件引用。"""
    __tablename__ = "cq_ai_call"
    __table_args__ = (
        UniqueConstraint("trace_id", "sequence", name="uq_cq_ai_call_sequence"),
        {**Base.__table_args__, "comment": "模型调用参数与结果状态"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="模型调用UUID")
    trace_id: Mapped[str] = mapped_column(ForeignKey("cq_ai_trace.id", ondelete="CASCADE"), comment="所属执行")
    sequence: Mapped[int] = mapped_column(Integer, comment="执行内的模型调用顺序，从1开始")
    model: Mapped[str] = mapped_column(String(200), comment="实际模型名称")
    status: Mapped[str] = mapped_column(String(20), default="running", comment="running/completed/failed/cancelled")
    request_body: Mapped[dict | None] = mapped_column(JSON, nullable=True, comment="最终请求JSON，图片替换为附件引用")
    images: Mapped[list[dict]] = mapped_column(JSON, default=list, comment="图片附件资料和原始数据地址摘要")
    capture_error: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="请求捕获不完整时的说明")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上游失败时的HTTP状态码")
    error: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="安全错误说明，不保存供应商原始异常正文")
    elapsed_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="模型调用耗时毫秒")
    created_at: Mapped[datetime] = mapped_column(TRACE_TIME, default=utc_now, comment="开始时间UTC")
    finished_at: Mapped[datetime | None] = mapped_column(TRACE_TIME, nullable=True, comment="结束时间UTC")
