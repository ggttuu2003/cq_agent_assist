"""验证真实 SSE 分段、停止、保存和重复请求，模型服务使用本地模拟传输。"""

import asyncio
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError

from app import dependencies as conversation_dependencies
from app.api import conversations as conversation_api
from app.db.models import Conversation, User, utc_now
from app.dependencies import get_conversation_agent
from app.llm.errors import ModelError
from app.main import protect_local_requests
from app.services.streaming import ConversationStreamResponse
from tests import test_conversations as fixtures


def parse_events(text):
    """把响应中的完整 SSE 帧转成事件名与数据，心跳注释不算业务事件。"""
    events = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and data:
            events.append((event, json.loads(data)))
    return events


class StreamingAgent:
    """通过事件控制首字或后续片段，验证服务端不会等完整回答才输出。"""

    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.allow_first = None
        self.allow_rest = None
        self.failure = False

    async def astream(self, state, *, stream_mode):
        if stream_mode != ["messages", "updates"]:
            raise AssertionError("流式回答需要同时读取消息片段和节点结果")
        self.calls.append(state["messages"])
        self.started.set()
        try:
            if self.allow_first:
                await self.allow_first.wait()
            yield "messages", (AIMessageChunk(content="你好😀"), {"langgraph_node": "model"})
            if self.allow_rest:
                await self.allow_rest.wait()
            if self.failure:
                raise ModelError("模拟模型失败")
            yield "messages", (AIMessageChunk(content="，这是流式回答。"), {"langgraph_node": "model"})
            yield "updates", {"model": {"messages": [AIMessage(content="你好😀，这是流式回答。")]}}
        finally:
            self.closed.set()


class StreamConnection:
    """直接驱动 ASGI 收发，观察真实响应分块和断开，不启动后台服务器。"""

    def __init__(self, app, conversation_id, body, *, drop_done=False):
        self.app = app
        self.path = f"/conversations/{conversation_id}/messages/stream"
        self.body = body
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.drop_done = drop_done
        self.task = None

    async def receive(self):
        return await self.incoming.get()

    async def send(self, message):
        if self.drop_done and b"event: done" in message.get("body", b""):
            raise OSError("模拟完成事件发送前连接断开")
        await self.outgoing.put(message)

    async def start(self):
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": self.path, "raw_path": self.path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "client": ("test", 1234), "server": ("test", 80),
        }
        await self.incoming.put({"type": "http.request", "body": json.dumps(self.body).encode(), "more_body": False})
        self.task = asyncio.create_task(self.app(scope, self.receive, self.send))
        header = await asyncio.wait_for(self.outgoing.get(), 5)
        assert header["type"] == "http.response.start" and header["status"] == 200, header
        return self

    async def next_body(self):
        while True:
            message = await asyncio.wait_for(self.outgoing.get(), 5)
            if message.get("body"):
                return message["body"].decode()

    async def disconnect(self):
        await self.incoming.put({"type": "http.disconnect"})
        if self.task:
            await asyncio.wait_for(self.task, 5)


@unittest.skipUnless(os.getenv("CQ_TEST_MYSQL") == "1", "设置 CQ_TEST_MYSQL=1 运行临时 MySQL 流式测试")
class TestStreaming(unittest.IsolatedAsyncioTestCase):
    """复用隔离临时库准备逻辑，所有断开测试都经过实际应用的 HTTP 中间件。"""

    asyncTearDown = fixtures.TestMySQLConversations.asyncTearDown
    prepare_conversation = fixtures.TestMySQLConversations.prepare_conversation

    async def asyncSetUp(self):
        await fixtures.TestMySQLConversations.asyncSetUp(self)
        self.app.middleware("http")(protect_local_requests)
        self.agent = StreamingAgent()

    async def stream(self, cid, body):
        return await self.client.post(f"/conversations/{cid}/messages/stream", json=body)

    async def test_complete_replay_and_context(self):
        """完成事件只在入库后返回；重复请求重放，下一轮带上已保存的上下文。"""
        cid = await self.prepare_conversation("初始标题")
        body = {"message": "你好", "request_id": str(uuid4())}
        response = await self.stream(cid, body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = parse_events(response.text)
        self.assertEqual([event for event, _ in events], ["delta", "delta", "done"])
        self.assertEqual("".join(data["text"] for event, data in events if event == "delta"), "你好😀，这是流式回答。")
        done = events[-1][1]
        self.assertNotIn("title", done)
        self.assertEqual(len(done["messages"]), 2)
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(saved["messages"], done["messages"])
        self.assertEqual(saved["title"], "初始标题")
        replay = parse_events((await self.stream(cid, body)).text)
        self.assertEqual([event for event, _ in replay], ["delta", "done"])
        self.assertEqual(replay[-1][1]["messages"], done["messages"])
        self.assertEqual(len(self.agent.calls), 1)
        self.assertEqual((await self.stream(cid, {**body, "message": "冲突问题"})).status_code, 409)
        await self.stream(cid, {"message": "继续", "request_id": str(uuid4())})
        self.assertEqual(self.agent.calls[-1], [("user", "你好"), ("assistant", "你好😀，这是流式回答。"), ("user", "继续")])
        self.assertEqual((await self.stream(str(uuid4()), body)).status_code, 404)
        self.assertEqual((await self.stream(cid, {"message": " ", "request_id": str(uuid4())})).status_code, 422)
        schema = (await self.client.get("/openapi.json")).json()
        self.assertIn("text/event-stream", schema["paths"]["/conversations/{conversation_id}/messages/stream"]["post"]["responses"]["200"]["content"])

    async def test_failure_and_retry(self):
        """收到部分内容后失败不保存半轮，原请求编号可以重试成功。"""
        cid = await self.prepare_conversation()
        body = {"message": "测试失败", "request_id": str(uuid4())}
        self.agent.failure = True
        events = parse_events((await self.stream(cid, body)).text)
        self.assertEqual([event for event, _ in events], ["delta", "error"])
        self.assertEqual(events[-1][1]["code"], "model_error")
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertFalse(saved["generating"])
        self.assertEqual(saved["messages"], [])
        self.agent.failure = False
        self.assertEqual(parse_events((await self.stream(cid, body)).text)[-1][0], "done")
        self.assertEqual(len((await self.client.get(f"/conversations/{cid}")).json()["messages"]), 2)

    async def test_stop_before_first_chunk_and_after_partial(self):
        """首字前和中途停止都关闭上游，等待期间不占数据库连接或保存半轮。"""
        for before_first in [True, False]:
            with self.subTest(before_first=before_first):
                self.agent = StreamingAgent()
                gate = asyncio.Event()
                if before_first:
                    self.agent.allow_first = gate
                else:
                    self.agent.allow_rest = gate
                cid = await self.prepare_conversation()
                body = {"message": "停止测试", "request_id": str(uuid4())}
                connection = await StreamConnection(self.app, cid, body).start()
                try:
                    await asyncio.wait_for(self.agent.started.wait(), 5)
                    if not before_first:
                        self.assertIn("event: delta", await connection.next_body())
                    self.assertEqual(self.engine.pool.checkedout(), 0)
                    saved = (await self.client.get(f"/conversations/{cid}")).json()
                    self.assertTrue(saved["generating"])
                    self.assertEqual(saved["messages"], [])
                    self.assertEqual((await self.stream(cid, {**body, "request_id": str(uuid4())})).status_code, 409)
                finally:
                    await connection.disconnect()
                self.assertTrue(self.agent.closed.is_set())
                saved = (await self.client.get(f"/conversations/{cid}")).json()
                self.assertFalse(saved["generating"])
                self.assertEqual(saved["messages"], [])

    async def test_heartbeat_and_timeout(self):
        """首字等待期间有心跳，总时限到达后发送错误并释放占用。"""
        cid = await self.prepare_conversation()
        self.agent.allow_first = asyncio.Event()
        original = conversation_dependencies.prepare_generation

        async def short_generation(*args):
            generation = await original(*args)
            generation.duration = 0.08
            return generation

        with patch.object(conversation_dependencies, "prepare_generation", short_generation), \
                patch.object(ConversationStreamResponse, "heartbeat_seconds", 0.02):
            response = await self.stream(cid, {"message": "超时", "request_id": str(uuid4())})
        self.assertIn(": ping\n\n", response.text)
        self.assertEqual(parse_events(response.text)[-1][1]["code"], "timeout")
        self.assertTrue(self.agent.closed.is_set())
        self.assertFalse((await self.client.get(f"/conversations/{cid}")).json()["generating"])

    async def test_save_failure_has_no_done_or_partial_messages(self):
        """保存失败只有错误事件，不能把已显示的文字当成已保存回答。"""
        cid = await self.prepare_conversation()
        with patch("app.services.streaming.save_generation", side_effect=SQLAlchemyError("内部数据库信息")):
            response = await self.stream(cid, {"message": "保存失败", "request_id": str(uuid4())})
        events = parse_events(response.text)
        self.assertEqual(events[-1][1]["code"], "save_error")
        self.assertNotIn("done", [event for event, _ in events])
        self.assertNotIn("内部数据库信息", response.text)
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(saved["messages"], [])
        self.assertFalse(saved["generating"])

    async def test_lost_done_preserves_committed_round(self):
        """数据库已提交但完成事件丢失时，详情查询和重试仍能恢复同一轮。"""
        cid = await self.prepare_conversation()
        body = {"message": "丢失完成事件", "request_id": str(uuid4())}
        # 中间件在内部转发发送异常，因此直接用没有中间件的同路由应用模拟 send 报错。
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(conversation_api.router)
        app.state = self.app.state
        app.dependency_overrides[get_conversation_agent] = lambda: self.agent
        connection = await StreamConnection(app, cid, body, drop_done=True).start()
        await asyncio.wait_for(connection.task, 5)
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(len(saved["messages"]), 2)
        replay = parse_events((await self.stream(cid, body)).text)
        self.assertEqual(replay[-1][1]["messages"], saved["messages"])
        self.assertEqual(len(self.agent.calls), 1)

    async def test_stop_during_save_rolls_back_uncommitted_transaction(self):
        """保存期间仍发送心跳，停止会回滚尚未提交的事务并释放占用。"""
        cid = await self.prepare_conversation()
        saving = asyncio.Event()

        async def blocked_save(db, generation, reply):
            db.add(User(id=2, display_name="仅用于验证事务回滚"))
            await db.flush()
            saving.set()
            await asyncio.Event().wait()

        with patch("app.services.streaming.save_generation", blocked_save), \
                patch.object(ConversationStreamResponse, "heartbeat_seconds", 0.02):
            connection = await StreamConnection(self.app, cid, {"message": "保存中停止", "request_id": str(uuid4())}).start()
            try:
                await asyncio.wait_for(saving.wait(), 5)
                body = ""
                while ": ping" not in body:
                    body += await connection.next_body()
                self.assertNotIn("event: done", body)
            finally:
                await connection.disconnect()
        async with self.sessions() as db:
            self.assertIsNone(await db.get(User, 2))
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertFalse(saved["generating"])
        self.assertEqual(saved["messages"], [])

    async def test_stale_cleanup_cannot_release_new_lease(self):
        """旧流断开时，仅释放自己的占用，不能清理后来请求的占用。"""
        cid = await self.prepare_conversation()
        self.agent.allow_first = asyncio.Event()
        connection = await StreamConnection(self.app, cid, {"message": "旧请求", "request_id": str(uuid4())}).start()
        new_lease = str(uuid4())
        try:
            await asyncio.wait_for(self.agent.started.wait(), 5)
            async with self.sessions() as db:
                row = await db.get(Conversation, cid)
                row.active_request = new_lease
                row.busy_until = utc_now() + timedelta(minutes=1)
                await db.commit()
        finally:
            await connection.disconnect()
        async with self.sessions() as db:
            self.assertEqual((await db.get(Conversation, cid)).active_request, new_lease)

    async def test_real_langgraph_streams_before_upstream_finishes(self):
        """真实图和兼容客户端启用 stream，首个片段在上游结束前到达页面连接。"""
        gate = asyncio.Event()
        upstream_closed = asyncio.Event()
        calls = []

        class Upstream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for index, text in enumerate(["真实片段", "结束"]):
                    if index:
                        await gate.wait()
                    payload = {"id": "stream-test", "object": "chat.completion.chunk", "created": 0,
                               "model": "test-model", "choices": [{"index": 0,
                               "delta": {"content": text}, "finish_reason": None}]}
                    yield ("data: " + json.dumps(payload) + "\n\n").encode()
                yield b"data: [DONE]\n\n"

            async def aclose(self):
                upstream_closed.set()

        def upstream(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Upstream())

        self.app.dependency_overrides.pop(get_conversation_agent)
        self.app.state.settings = self.app.state.settings.model_copy(update={
            "llm_base_url": "https://example.com/v1", "llm_api_key": SecretStr("test-key"),
        })
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            self.app.state.http_client = client
            cid = await self.prepare_conversation()
            connection = await StreamConnection(self.app, cid, {"message": "真实图", "request_id": str(uuid4())}).start()
            try:
                first = await connection.next_body()
                self.assertEqual(parse_events(first)[0][1]["text"], "真实片段")
                self.assertFalse(gate.is_set())
                self.assertTrue(calls[0]["stream"])
                self.assertEqual(calls[0]["model"], "test-model")
                self.assertEqual(self.engine.pool.checkedout(), 0)
                gate.set()
                await asyncio.wait_for(connection.task, 5)
            finally:
                await connection.disconnect()
            # 再开启一轮后中途停止，确认真实 SDK 的上游流也会关闭。
            self.assertTrue(upstream_closed.is_set())
            upstream_closed.clear()
            gate = asyncio.Event()
            connection = await StreamConnection(self.app, cid, {"message": "停止真实图", "request_id": str(uuid4())}).start()
            try:
                self.assertEqual(parse_events(await connection.next_body())[0][0], "delta")
            finally:
                await connection.disconnect()
        self.assertTrue(upstream_closed.is_set())
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(len(saved["messages"]), 2)
        self.assertFalse(saved["generating"])
        self.assertEqual(saved["messages"][-1]["content"], "真实片段结束")


if __name__ == "__main__":
    unittest.main()
