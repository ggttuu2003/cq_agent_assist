"""会话和消息接口使用的数据结构。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from app.schemas.attachments import AttachmentInfo
from app.schemas.knowledge import Citation


class CreateConversationRequest(BaseModel):
    """首次问题用于生成初始标题，消息仍由发送接口保存。"""
    first_message: str = Field(min_length=1, max_length=20000)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    knowledge_enabled: bool = Field(default=False, strict=True)

    @field_validator("model_name", mode="before")
    @classmethod
    def trim_model_name(cls, value):
        """显式选择模型时，去掉首尾空格再校验。"""
        return value.strip() if isinstance(value, str) else value

    @field_validator("first_message", mode="before")
    @classmethod
    def trim_first_message(cls, value):
        """去掉首尾空格后校验，避免创建没有有效标题的会话。"""
        return value.strip() if isinstance(value, str) else value


class SendMessageRequest(BaseModel):
    """每次发送生成独立请求编号，网络重试时复用该编号。"""
    message: str = Field(default="", max_length=20000)
    request_id: UUID
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_message(self) -> "SendMessageRequest":
        """只发图片时统一补充问题，各客户端无需各自约定默认文案。"""
        self.message = self.message.strip()
        if not self.message and self.attachment_ids:
            self.message = "请描述这些图片"
        if not self.message:
            raise ValueError("消息不能为空")
        return self


class ConversationTitleUpdate(BaseModel):
    """修改会话标题，长度与数据库标题字段一致。"""
    title: str = Field(min_length=1, max_length=120)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value):
        """先去掉首尾空格，再校验是否为空和长度。"""
        return value.strip() if isinstance(value, str) else value


class ConversationModelUpdate(BaseModel):
    """只更新会话绑定的模型，避免和标题修改混用。"""
    model_name: str = Field(min_length=1, max_length=200)

    @field_validator("model_name", mode="before")
    @classmethod
    def trim_model_name(cls, value):
        """先处理空格，拒绝空模型名称。"""
        return value.strip() if isinstance(value, str) else value


class ConversationModelInfo(BaseModel):
    """切换成功只返回模型字段，避免前端用旧快照覆盖正在更新的消息。"""
    id: str
    model_name: str
    image_input_supported: bool | None = None


class ConversationKnowledgeUpdate(BaseModel):
    """知识库开关由会话保存，拒绝把字符串当成布尔值。"""
    knowledge_enabled: bool = Field(strict=True)


class ConversationKnowledgeInfo(ConversationKnowledgeUpdate):
    """只返回已保存的开关，避免覆盖客户端正在显示的问答。"""
    id: str


class MessageInfo(BaseModel):
    """已保存的消息，同时记录本轮请求和实际使用的模型。"""
    id: str
    role: str
    content: str
    model: str | None
    request_id: str
    attachments: list[AttachmentInfo] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)


class ConversationInfo(BaseModel):
    """前端会话详情；所有时间显式带 UTC 时区。"""
    id: str
    title: str
    model_name: str
    knowledge_enabled: bool = False
    image_input_supported: bool | None = None
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
