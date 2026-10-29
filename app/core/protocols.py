from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class Document:
    """系统内部统一使用的文档对象。"""
    content:str
    metadata:dict[str,object] = field(default_factory=dict)

class SourceAdapter(Protocol):
    """数据来源适配器协议。"""

    async def load(self) -> list[Document]:
        """读取一种来源中的文档。"""
        ...


class Retriever(Protocol):
    """检索器协议。"""

    async def search(self, query: str) -> list[Document]:
        """根据问题检索文档。"""
        ...


class Tool(Protocol):
    """工具协议。"""

    async def run(self, arguments: dict[str, object]) -> object:
        """执行工具。"""
        ...
