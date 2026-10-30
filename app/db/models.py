"""个人资料、会话和消息的 MySQL 表结构。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """数据库统一保存不带时区的 UTC 时间，返回接口时再标明时区。"""
    return datetime.utcnow()


class Base(DeclarativeBase):
    """所有数据库模型的基类，统一指定表的字符集和存储引擎。"""
    __abstract__ = True
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class User(Base):
    """本地唯一的个人档案；昵称用于展示，不是登录凭据。"""
    __tablename__ = "cq_user"
    __table_args__ = {**Base.__table_args__, "comment": "本地个人资料"}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, comment="个人资料编号，固定为1")
    display_name: Mapped[str] = mapped_column(String(40), comment="页面显示的昵称")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, comment="创建时间，UTC")


class Conversation(Base):
    """会话归属和生成占用信息；占用有到期时间，进程中断后可恢复。"""
    __tablename__ = "cq_conversation"
    __table_args__ = (
        Index("ix_cq_conversation_user_updated", "user_id", "updated_at"),
        {**Base.__table_args__, "comment": "用户会话"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="会话UUID")
    user_id: Mapped[int] = mapped_column(ForeignKey("cq_user.id", ondelete="CASCADE"), comment="所属用户")
    title: Mapped[str] = mapped_column(String(120), default="新对话", comment="会话标题，创建时生成，可手动修改")
    model_name: Mapped[str] = mapped_column(String(200), default="", comment="本会话使用的模型")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, comment="创建时间，UTC")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, comment="最近完成对话时间，UTC")
    active_request: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="正在生成的请求UUID")
    busy_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="生成占用到期时间，UTC")
    messages: Mapped[list["Message"]] = relationship(
        order_by="Message.sequence", passive_deletes=True, lazy="raise",
    )


class Message(Base):
    """只保存成功完成的一轮问答，问题和回答在同一事务写入。"""
    __tablename__ = "cq_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence", name="uq_cq_message_sequence"),
        UniqueConstraint("conversation_id", "request_id", "role", name="uq_cq_message_request_role"),
        {**Base.__table_args__, "comment": "成功保存的对话消息"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="消息UUID")
    conversation_id: Mapped[str] = mapped_column(ForeignKey("cq_conversation.id", ondelete="CASCADE"), comment="所属会话")
    sequence: Mapped[int] = mapped_column(Integer, comment="会话内的消息顺序，从1开始")
    request_id: Mapped[str] = mapped_column(String(36), comment="本轮发送请求UUID，用于去重")
    role: Mapped[str] = mapped_column(String(16), comment="消息角色：user或assistant")
    content: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"), comment="消息正文")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="回答使用的模型，用户消息为空")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, comment="保存时间，UTC")
