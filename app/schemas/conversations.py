"""会话和消息接口使用的数据结构。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CreateConversationRequest(BaseModel):
    """首次问题用于生成初始标题，消息仍由发送接口保存。"""
    first_message: str = Field(min_length=1, max_length=20000)

    @field_validator("first_message", mode="before")
    @classmethod
    def trim_first_message(cls, value):
        """去掉首尾空格后校验，避免创建没有有效标题的会话。"""
        return value.strip() if isinstance(value, str) else value


class SendMessageRequest(BaseModel):
    """每次发送生成独立请求编号，网络重试时复用该编号。"""
    message: str = Field(min_length=1, max_length=20000)
    request_id: UUID

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """拒绝空白问题，避免创建无意义的模型调用。"""
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value


class ConversationTitleUpdate(BaseModel):
    """修改会话标题，长度与数据库标题字段一致。"""
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        """先去掉首尾空格，再校验是否为空和长度。"""
        return value.strip() if isinstance(value, str) else value


class MessageInfo(BaseModel):
    """已保存的消息，同时记录本轮请求和实际使用的模型。"""
    id: str
    role: str
    content: str
    model: str | None
    request_id: str


class ConversationInfo(BaseModel):
    """前端会话详情；所有时间显式带 UTC 时区。"""
    id: str
    title: str
    model_name: str
    updated_at: datetime
    messages: list[MessageInfo]
    generating: bool = False


class ConversationTurnInfo(BaseModel):
    """流式完成结果，只包含本轮消息，避免覆盖前端的会话标题等资料。"""
    request_id: str
    conversation_id: str
    updated_at: datetime
    messages: list[MessageInfo]
    generating: bool = False
