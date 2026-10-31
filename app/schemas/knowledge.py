"""知识资料接口与回答引用的数据结构。"""

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class Citation(BaseModel):
    """保存实际检索到的来源快照，不依赖资料后续是否更新或删除。"""
    number: int = Field(ge=1)
    document_id: str
    version_id: str
    chunk_id: str
    title: str
    location: str
    quote: str


class KnowledgeDocumentInfo(BaseModel):
    """列表和详情共用的资料摘要，正文通过独立接口分页读取。"""
    id: str
    name: str
    source_type: str
    status: str
    error: str | None
    active_version_id: str | None
    target_version_id: str | None
    size: int
    chunk_count: int
    completed: int = 0
    total: int = 0
    job_id: str | None = None
    updated_at: datetime


class KnowledgePage(BaseModel):
    """基于更新时间和编号的游标分页结果。"""
    items: list[KnowledgeDocumentInfo]
    next_cursor: str | None = None


class KnowledgeRename(BaseModel):
    """资料显示名的修改不触发重建索引。"""
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        """先去掉首尾空格，再校验空值。"""
        return value.strip() if isinstance(value, str) else value


class KnowledgeQuery(BaseModel):
    """页面试搜只传问题，所属用户由服务端注入。"""
    query: str = Field(min_length=1, max_length=400)

    @field_validator("query", mode="before")
    @classmethod
    def trim_query(cls, value):
        """避免全空格查询调用向量模型。"""
        return value.strip() if isinstance(value, str) else value
