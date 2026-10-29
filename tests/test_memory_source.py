"""测试内存数据源。"""

import unittest

from app.core.protocols import Document
from app.sources.memory import MemorySource


class TestMemorySource(unittest.IsolatedAsyncioTestCase):
    """支持异步方法的测试类。"""

    async def test_load_documents(self):
        """验证数据源能够返回传入的文档。"""

        # 1. 准备文档和数据源
        document = Document(
            content="FastAPI 是一个 Python Web 框架。",
            metadata={"title": "FastAPI 入门"},
        )
        source = MemorySource([document])

        # 2. 调用待测试的方法
        documents = await source.load()

        # 3. 检查结果是否符合预期
        self.assertEqual(len(documents), 1)
        self.assertEqual(
            documents[0].content,
            "FastAPI 是一个 Python Web 框架。",
        )
        self.assertEqual(
            documents[0].metadata["title"],
            "FastAPI 入门",
        )

if __name__ == "__main__":
    unittest.main()