"""内存数据来源，用于验证数据来源协议。"""

from collections.abc import Iterable

from app.core.protocols import Document


class MemorySource:
    """从内存中读取文档。"""

    def __init__(self, documents: Iterable[Document] | None = None) -> None:
        """接收一组初始文档。"""

        self._documents = list(documents or [])

    async def load(self) -> list[Document]:
        """返回一个新列表；列表中的 Document 对象仍与数据源共享。"""

        return list(self._documents)
