"""验证免费搜索适配器，以及停止、超时时真正结束工作进程，不访问外部搜索源。"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from app.core.settings import Settings
from app.search.base import SearchError
from app.search.ddgs import DdgsSearch, run_ddgs_search
from app.search.factory import create_search_provider
from app.search.tavily import TavilySearch


def blocking_search(marker: str):
    """在测试工作进程中记录自身编号并持续等待，父进程会主动停止或超时终止它。"""
    Path(marker).write_text(str(os.getpid()))
    time.sleep(30)
    return []


class TestDdgsResults(unittest.TestCase):
    """同步工作函数只处理 DDGS 的结果和错误，方便在 IDEA 中独立运行。"""

    def test_result_mapping_and_common_limits(self):
        """href/body 映射正确，过滤坏链接，去重并限制条数和摘要长度。"""
        rows = [
            {"title": "搜索结果", "href": "https://example.com/a b", "body": "摘" * 2000},
            {"title": "重复", "href": "https://example.com/a b", "body": "重复内容"},
            {"title": "非法", "href": "javascript:alert(1)", "body": "跳过"},
            None,
            {"title": "缺字段", "href": "https://example.com/missing"},
        ] + [{"title": str(index), "href": f"https://example.com/{index}", "body": "摘要"} for index in range(8)]
        with patch("app.search.ddgs.DDGS") as library:
            library.return_value.__enter__.return_value.text.return_value = rows
            results = run_ddgs_search("中文搜索")
            library.assert_called_once_with(timeout=10)
            library.return_value.__enter__.return_value.text.assert_called_once_with("中文搜索", max_results=5)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0].url, "https://example.com/a%20b")
        self.assertEqual(len(results[0].content), 1200)

    def test_errors_and_empty_results(self):
        """限流、超时、异常数据明确报错；空结果单独处理，不泄露库的原始错误。"""
        for error, expected in [(RatelimitException("private-response"), "限流"),
                                (TimeoutException("private-response"), "超时"),
                                (DDGSException("private-response"), "搜索失败")]:
            with self.subTest(error=type(error).__name__), patch("app.search.ddgs.DDGS") as library:
                library.return_value.__enter__.return_value.text.side_effect = error
                with self.assertRaisesRegex(SearchError, expected) as caught:
                    run_ddgs_search("查询")
                self.assertNotIn("private-response", str(caught.exception))
        with patch("app.search.ddgs.DDGS") as library:
            search = library.return_value.__enter__.return_value
            search.text.side_effect = DDGSException("No results found.")
            self.assertEqual(run_ddgs_search("没有结果"), [])
            search.text.side_effect = None
            search.text.return_value = []
            self.assertEqual(run_ddgs_search("没有结果"), [])
            search.text.return_value = {"unexpected": True}
            with self.assertRaisesRegex(SearchError, "格式"):
                run_ddgs_search("异常结果")


class TestDdgsExecution(unittest.IsolatedAsyncioTestCase):
    """验证配置切换及真实 AnyIO 工作进程的取消，搜索源使用可控测试函数。"""

    async def test_provider_switch_does_not_require_ddgs_key(self):
        """默认选择 DDGS，切回 Tavily 时继续使用原来的 Key 配置。"""
        async with httpx.AsyncClient() as client:
            settings = Settings(_env_file=None, tavily_api_key="")
            self.assertIsInstance(create_search_provider(settings, client), DdgsSearch)
            selected = settings.model_copy(update={"search_provider": " DDGS "})
            self.assertIsInstance(create_search_provider(selected, client), DdgsSearch)
            tavily = Settings(_env_file=None, search_provider="tavily", tavily_api_key="fake-key")
            self.assertIsInstance(create_search_provider(tavily, client), TavilySearch)

    @unittest.skipUnless(os.name == "posix", "进程存活检查使用 POSIX 信号，Windows 由 AnyIO 的进程终止实现负责")
    async def test_stop_kills_worker_without_blocking_event_loop(self):
        """取消搜索任务后工作进程已经退出，主事件循环在等待期间仍可运行。"""
        # unittest 的命令行入口导入后会直接启动测试；工作进程只需导入本模块的测试函数。
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.search.ddgs.run_ddgs_search", blocking_search), \
                patch.object(sys.modules["__main__"], "__file__", None, create=True):
            marker = Path(directory) / "search.pid"
            task = asyncio.create_task(DdgsSearch().search(str(marker)))
            try:
                async with asyncio.timeout(5):
                    while not marker.exists():
                        await asyncio.sleep(0.02)
                pid = int(marker.read_text())
                self.assertNotEqual(pid, os.getpid())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_total_timeout_returns_chinese_error(self):
        """总超时能够结束阻塞搜索，并转换为页面和模型可以理解的错误。"""
        with tempfile.TemporaryDirectory() as directory, \
                patch("app.search.ddgs.run_ddgs_search", blocking_search), \
                patch.object(sys.modules["__main__"], "__file__", None, create=True), \
                patch.object(DdgsSearch, "timeout_seconds", 1):
            marker = Path(directory) / "search.pid"
            with self.assertRaisesRegex(SearchError, "搜索超时"):
                await DdgsSearch().search(str(marker))
            if os.name == "posix" and marker.exists():
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(marker.read_text()), 0)


if __name__ == "__main__":
    unittest.main()
