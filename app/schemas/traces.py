"""调用详情接口的数据结构，普通消息接口不携带大段请求内容。"""

from datetime import datetime

from pydantic import BaseModel, Field


class ModelCallInfo(BaseModel):
    """按实际发送顺序展示的一次模型请求。"""
    id: str
    sequence: int
    model: str
    status: str
    request_body: dict | None
    images: list[dict]
    capture_error: str | None
    http_status: int | None
    error: str | None
    elapsed_ms: int | None
    created_at: datetime
    finished_at: datetime | None


class TraceAttemptInfo(BaseModel):
    """一次执行及其检索快照；同一问题可以包含多次失败重试。"""
    id: str
    http_request_id: str
    model: str
    knowledge_enabled: bool
    status: str
    retrieval: dict | None
    created_at: datetime
    finished_at: datetime | None
    calls: list[ModelCallInfo] = Field(default_factory=list)


class RequestTraceInfo(BaseModel):
    """没有记录时返回空列表，不把旧消息推算成模型调用。"""
    conversation_id: str
    request_id: str
    attempts: list[TraceAttemptInfo] = Field(default_factory=list)
