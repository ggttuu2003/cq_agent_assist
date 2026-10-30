"""验证请求关联、流式计时和异常日志，不连接数据库或真实模型。"""

import asyncio
import io
import logging
import sys
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.core.logging import (
    RequestLoggingMiddleware, SafeFormatter, configure_logging, request_id_context,
)


class TestLogging(unittest.IsolatedAsyncioTestCase):
    """每项测试隔离日志配置，避免影响其他测试和开发服务。"""

    def setUp(self):
        self.app_logger = logging.getLogger("app")
        self.previous = (self.app_logger.handlers[:], self.app_logger.level, self.app_logger.propagate)
        self.output = io.StringIO()
        with patch("sys.stdout", self.output):
            configure_logging()
        self.logger = logging.getLogger("app.tests")

    def tearDown(self):
        self.app_logger.handlers, level, self.app_logger.propagate = self.previous
        self.app_logger.setLevel(level)

    async def test_concurrent_requests_keep_separate_ids_and_reset_context(self):
        """并发请求和子任务使用自己的编号，响应头可关联日志，完成后恢复默认上下文。"""
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        seen = []
        ready, release = asyncio.Event(), asyncio.Event()

        async def work():
            seen.append(request_id_context.get())
            if len(seen) == 2:
                ready.set()
            await release.wait()
            return {"request_id": request_id_context.get()}

        app.get("/work")(work)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            calls = [asyncio.create_task(client.get("/work")) for _ in range(2)]
            try:
                await asyncio.wait_for(ready.wait(), 3)
                release.set()
                responses = await asyncio.gather(*calls)
            finally:
                release.set()
                await asyncio.gather(*calls, return_exceptions=True)
        ids = [response.headers["x-request-id"] for response in responses]
        self.assertEqual(len(set(ids)), 2)
        for response in responses:
            self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])
            self.assertIn("请求=" + response.headers["x-request-id"], self.output.getvalue())
        self.assertEqual(request_id_context.get(), "-")

    async def test_stream_is_logged_after_last_body_not_headers(self):
        """发出流式响应头时不打印完成日志，最后一个片段发送成功后才结束计时。"""
        headers_sent, finish = asyncio.Event(), asyncio.Event()
        messages = []

        async def streaming(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            headers_sent.set()
            await finish.wait()
            await send({"type": "http.response.body", "body": b"last", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(message):
            messages.append(message)

        app = RequestLoggingMiddleware(streaming)
        with patch("app.core.logging.perf_counter", side_effect=[10, 12.5]):
            task = asyncio.create_task(app({"type": "http", "method": "POST", "path": "/stream"}, receive, send))
            try:
                await asyncio.wait_for(headers_sent.wait(), 3)
                self.assertNotIn("请求结束", self.output.getvalue())
            finally:
                finish.set()
                await task
        self.assertIn("耗时=2500ms", self.output.getvalue())
        self.assertIn("结果=完成", self.output.getvalue())
        self.assertTrue(any(key == b"x-request-id" for key, _ in messages[0]["headers"]))

    async def test_disconnect_and_unhandled_error_have_end_logs(self):
        """断开与未处理异常都留下结束记录，同时不吞掉原始异常。"""
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        async def disconnected(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await receive()

        async def broken(scope, receive, send):
            raise RuntimeError("private-provider-body")

        scope = {"type": "http", "method": "POST", "path": "/stream"}
        await RequestLoggingMiddleware(disconnected)(scope, receive, send)
        self.assertIn("结果=客户端断开", self.output.getvalue())
        with self.assertRaises(RuntimeError):
            await RequestLoggingMiddleware(broken)(scope, receive, send)
        output = self.output.getvalue()
        self.assertIn("status=500", output)
        self.assertIn("结果=异常", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("private-provider-body", output)
        self.assertEqual(request_id_context.get(), "-")

    async def test_request_metadata_excludes_headers_query_and_body(self):
        """只记录方法、路径和状态，敏感请求数据不进入应用日志。"""
        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)

        async def endpoint():
            return {"reply": "private-answer"}

        app.post("/test")(endpoint)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/test?token=private-query", headers={"authorization": "private-key"},
                                         json={"message": "private-question"})
        self.assertEqual(response.status_code, 200)
        output = self.output.getvalue()
        for secret in ["private-key", "private-query", "private-question", "private-answer"]:
            self.assertNotIn(secret, output)

    async def test_formatter_excludes_sql_parameters_and_config_is_idempotent(self):
        """SQL 异常只记录类型与位置，重复配置不会使日志打印两次。"""
        from sqlalchemy.exc import StatementError

        try:
            raise StatementError("private-error", "INSERT private-sql", {"password": "private-password"},
                                 ValueError("private-original"))
        except StatementError:
            text = SafeFormatter().formatException(sys.exc_info())
        self.assertIn("StatementError", text)
        self.assertIn("test_logging.py:", text)
        self.assertNotIn("private-", text)
        with patch("sys.stdout", self.output):
            configure_logging("DEBUG")
            configure_logging("DEBUG")
        self.logger.debug("唯一调试记录")
        self.assertEqual(self.output.getvalue().count("唯一调试记录"), 1)
        self.assertIn("请求=-", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
