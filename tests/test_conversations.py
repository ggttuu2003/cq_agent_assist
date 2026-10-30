"""会话上下文及 MySQL 集成测试，不使用真实模型或用户数据库中的数据。"""

import asyncio
import json
import os
import unittest
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from sqlalchemy import func, select, text

from app.api.conversations import router as conversations_router
from app.dependencies import get_conversation_agent
from app.api.chat import router as chat_router
from app.api.profile import router as profile_router
from app.core.settings import get_settings
from app.db.database import create_database, initialize_database
from app.db.models import Conversation, Message, User, utc_now
from app.llm.errors import ModelError
from app.services.conversations import model_history


class RecordingAgent:
    """记录每次调用收到的上下文，并按测试需要模拟失败或暂停。"""

    def __init__(self):
        self.calls = []
        self.failure = False
        self.started = None
        self.release = None

    async def ainvoke(self, state):
        self.calls.append(state["messages"])
        if self.started:
            self.started.set()
            await self.release.wait()
        if self.failure:
            raise ModelError("模拟模型失败")
        return {"messages": [AIMessage(content="测试回答")]}


class TestHistory(unittest.TestCase):
    """不需要连接数据库即可验证历史裁剪不会拆散问答。"""

    def test_recent_context_keeps_complete_rounds(self):
        rows = [Message(role="user", content="旧问题"), Message(role="assistant", content="x" * 60000),
                Message(role="user", content="新问题"), Message(role="assistant", content="新回答")]
        self.assertEqual(model_history(rows), [("user", "新问题"), ("assistant", "新回答")])


@unittest.skipUnless(os.getenv("CQ_TEST_MYSQL") == "1", "设置 CQ_TEST_MYSQL=1 运行临时 MySQL 数据库测试")
class TestMySQLConversations(unittest.IsolatedAsyncioTestCase):
    """每个测试建立独立的临时库，结束后仅删除自己创建的库。"""

    async def asyncSetUp(self):
        settings = get_settings()
        self.database_name = "cq_test_" + uuid4().hex
        self.admin, _ = create_database(settings.model_copy(update={"mysql_db": "mysql"}))
        async with self.admin.begin() as connection:
            await connection.execute(text(f"CREATE DATABASE `{self.database_name}` CHARACTER SET utf8mb4"))
        self.engine, self.sessions = create_database(settings.model_copy(update={"mysql_db": self.database_name}))
        await initialize_database(self.engine)
        self.agent = RecordingAgent()
        self.app = FastAPI()
        self.app.include_router(profile_router)
        self.app.include_router(conversations_router)
        self.app.include_router(chat_router)
        self.app.state.db_sessions = self.sessions
        self.app.state.settings = settings.model_copy(update={"llm_model": "test-model"})
        self.app.state.agents = {}
        self.app.dependency_overrides[get_conversation_agent] = lambda: self.agent
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.engine.dispose()
        async with self.admin.begin() as connection:
            await connection.execute(text(f"DROP DATABASE `{self.database_name}`"))
        await self.admin.dispose()

    async def prepare_conversation(self, first_message="你好"):
        """先建立个人资料，再按首次问题创建带标题的会话。"""
        await self.client.put("/profile", json={"display_name": "测试用户"})
        response = await self.client.post("/conversations", json={"first_message": first_message})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def send(self, conversation_id, question="你好", request_id=None):
        """每次真实发送使用新编号，重试显式复用原编号。"""
        return await self.client.post(f"/conversations/{conversation_id}/messages", json={
            "message": question, "request_id": request_id or str(uuid4()),
        })

    async def test_profile_and_required_setup(self):
        """首次读取自动得到唯一默认昵称，之后仍可修改。"""
        first = (await self.client.get("/profile")).json()
        self.assertEqual(first["id"], 1)
        self.assertRegex(first["display_name"], r"^CQ用户-[0-9A-F]{6}$")
        self.assertEqual((await self.client.get("/conversations")).status_code, 200)
        self.assertEqual((await self.client.get("/profile")).json(), first)
        for name in ["", "   ", "x" * 41]:
            self.assertEqual((await self.client.put("/profile", json={"display_name": name})).status_code, 422)
        response = await self.client.put("/profile", json={"display_name": "  长青  "})
        self.assertEqual(response.json(), {"id": 1, "display_name": "长青"})
        await self.client.put("/profile", json={"display_name": "新的昵称"})
        self.assertEqual((await self.client.get("/profile")).json()["display_name"], "新的昵称")
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(User)), 1)

    async def test_persistence_history_and_idempotency(self):
        """成功回答存入真实 MySQL；后续请求带历史，重试不重复保存和调用。"""
        cid = await self.prepare_conversation("记住我叫长青")
        request_id = str(uuid4())
        first = await self.send(cid, "记住我叫长青", request_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["title"], "记住我叫长青")
        self.assertEqual(first.json()["model_name"], "test-model")
        self.assertEqual(len(first.json()["messages"]), 2)
        self.assertEqual(first.json()["messages"][1]["model"], "test-model")
        repeated = await self.send(cid, "记住我叫长青", request_id)
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(len(self.agent.calls), 1)
        collision = await self.send(cid, "另一个问题", request_id)
        self.assertEqual(collision.status_code, 409)
        second = await self.send(cid, "我叫什么")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(self.agent.calls[-1], [("user", "记住我叫长青"), ("assistant", "测试回答"), ("user", "我叫什么")])
        # 新的 HTTP 客户端和数据库会话依然读到相同记录，不依赖前端内存。
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            self.assertEqual((await client.get(f"/conversations/{cid}")).json(), second.json())
        self.assertEqual(len((await self.client.get("/conversations")).json()), 1)

    async def test_initial_title_persists_before_model_call_and_after_failure(self):
        """创建时标题已入库；首次回答失败、重试或继续对话均不会改变标题。"""
        question = "学习 Python 异步与 LangGraph 会话管理，标题需要在创建时保存 😀"
        cid = await self.prepare_conversation(f"  {question}  ")
        path = f"/conversations/{cid}"
        expected_title = question[:28]
        saved = (await self.client.get(path)).json()
        self.assertEqual(saved["title"], expected_title)
        self.assertEqual(saved["messages"], [])
        self.assertEqual(self.agent.calls, [])
        async with self.sessions() as db:
            self.assertEqual((await db.get(Conversation, cid)).title, expected_title)

        self.agent.failure = True
        self.assertEqual((await self.send(cid, question)).status_code, 502)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            reloaded = (await client.get(path)).json()
            self.assertEqual(reloaded["title"], expected_title)
            self.assertEqual(reloaded["messages"], [])
            self.assertFalse(reloaded["generating"])
            self.assertEqual((await client.get("/conversations")).json()[0]["title"], expected_title)

        self.agent.failure = False
        # 即使标题恰好叫“新对话”，发送接口也不能把它识别成需要重新生成的标题。
        self.assertEqual((await self.client.patch(path, json={"title": "新对话"})).status_code, 200)
        self.assertEqual((await self.send(cid, "换一个问题重试")).json()["title"], "新对话")
        self.assertEqual((await self.send(cid, "继续学习")).json()["title"], "新对话")

        for body in [{}, {"first_message": ""}, {"first_message": "   "},
                     {"first_message": "长" * 20001}]:
            self.assertEqual((await self.client.post("/conversations", json=body)).status_code, 422)
        self.assertEqual(len((await self.client.get("/conversations")).json()), 1)

    async def test_rename_persists_without_changing_messages_or_model(self):
        """标题去空格、持久保存；首轮与后续问答均保留自定义标题。"""
        cid = await self.prepare_conversation()
        path = f"/conversations/{cid}"
        for title in ["", "   ", "长" * 121]:
            self.assertEqual((await self.client.patch(path, json={"title": title})).status_code, 422)
        renamed = await self.client.patch(path, json={"title": "  Python 学习笔记  "})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["title"], "Python 学习笔记")
        first = (await self.send(cid, "介绍一下异步")).json()
        self.assertEqual(first["title"], "Python 学习笔记")
        renamed = (await self.client.patch(path, json={"title": "异步入门"})).json()
        self.assertEqual(renamed["messages"], first["messages"])
        self.assertEqual(renamed["model_name"], first["model_name"])
        self.assertEqual((await self.send(cid, "再举个例子")).json()["title"], "异步入门")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            self.assertEqual((await client.get(path)).json()["title"], "异步入门")
            self.assertEqual((await client.get("/conversations")).json()[0]["title"], "异步入门")
        self.assertEqual((await self.client.patch(f"/conversations/{uuid4()}", json={"title": "不存在"})).status_code, 404)

    async def test_conversation_model_controls_real_graph_call(self):
        """更改默认模型后，实际模型请求、会话详情和消息记录仍各自匹配。"""
        calls = []
        unavailable = False

        def handle(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if unavailable:
                return httpx.Response(404, json={"error": "测试模型不可用"})
            return httpx.Response(200, json={
                "id": "test-completion", "object": "chat.completion", "created": 0,
                "model": payload["model"],
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": f"来自 {payload['model']} 的回答",
                }}],
            })

        self.app.dependency_overrides.pop(get_conversation_agent)
        self.app.state.settings = self.app.state.settings.model_copy(update={
            "llm_model": "model-a", "llm_base_url": "https://example.com/v1",
            "llm_api_key": SecretStr("test-key"),
        })
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
            self.app.state.http_client = upstream
            first_id = await self.prepare_conversation()
            self.assertEqual((await self.send(first_id)).status_code, 200)

            self.app.state.settings = self.app.state.settings.model_copy(update={"llm_model": "model-b"})
            self.assertEqual((await self.client.get("/models/default")).json(), {"model": "model-b"})
            second_id = (await self.client.post("/conversations", json={"first_message": "你好"})).json()["id"]
            first = await self.send(first_id, "继续第一个会话")
            second = await self.send(second_id, "新的问题")
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(first.json()["model_name"], "model-a")
            self.assertEqual(second.json()["model_name"], "model-b")
            self.assertEqual([call["model"] for call in calls], ["model-a", "model-a", "model-b"])
            for data, name in [(first.json(), "model-a"), (second.json(), "model-b")]:
                self.assertTrue(all(message["model"] == name for message in data["messages"] if message["role"] == "assistant"))
                self.assertEqual((await self.client.get(f"/conversations/{data['id']}")).json(), data)
            listed = {item["id"]: item["model_name"] for item in (await self.client.get("/conversations")).json()}
            self.assertEqual(listed, {first_id: "model-a", second_id: "model-b"})

            # 默认模型变空仍可使用已绑定模型；新会话给出配置提示。
            self.app.state.settings = self.app.state.settings.model_copy(update={"llm_model": ""})
            self.assertEqual((await self.send(first_id, "使用已绑定模型")).status_code, 200)
            self.assertEqual((await self.client.post("/conversations", json={"first_message": "你好"})).status_code, 503)
            self.assertEqual(calls[-1]["model"], "model-a")

            # 会话模型不可用时明确失败，不切换到另一个模型或保存半轮消息。
            unavailable = True
            self.app.state.settings = self.app.state.settings.model_copy(update={"llm_model": "model-b"})
            failed = await self.send(first_id, "模拟不可用")
            self.assertEqual(failed.status_code, 502, failed.text)
            self.assertEqual(calls[-1]["model"], "model-a")
            saved = (await self.client.get(f"/conversations/{first_id}")).json()
            self.assertEqual(saved["model_name"], "model-a")
            self.assertFalse(saved["generating"])
            self.assertEqual(len(saved["messages"]), 6)

    async def test_existing_conversation_model_migration(self):
        """旧表补列后优先恢复历史模型，空会话用默认值，重复启动不覆盖。"""
        first_id = await self.prepare_conversation()
        empty_id = (await self.client.post("/conversations", json={"first_message": "你好"})).json()["id"]
        async with self.sessions() as db:
            db.add_all([
                Message(id=str(uuid4()), conversation_id=first_id, sequence=1,
                        request_id=str(uuid4()), role="assistant", content="早期回答", model="old-model"),
                Message(id=str(uuid4()), conversation_id=first_id, sequence=2,
                        request_id=str(uuid4()), role="assistant", content="最近回答", model="last-model"),
            ])
            await db.commit()
        # 只在当前测试创建的临时库中模拟尚未有模型字段的旧版本表。
        async with self.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE cq_conversation DROP COLUMN model_name"))
        await initialize_database(self.engine, "fallback-model")
        await initialize_database(self.engine, "changed-default")
        async with self.sessions() as db:
            self.assertEqual((await db.get(Conversation, first_id)).model_name, "last-model")
            self.assertEqual((await db.get(Conversation, empty_id)).model_name, "fallback-model")
            self.assertEqual(await db.scalar(select(func.count()).select_from(Message)), 2)

    async def test_ownership_and_delete_cascade(self):
        """查询和删除都按个人档案过滤，删除自己的会话时级联清理消息。"""
        cid = await self.prepare_conversation()
        await self.send(cid)
        other_id = str(uuid4())
        async with self.sessions() as db:
            # 只在隔离测试库构造另一份资料，以验证预留的关联查询不会漏过滤条件。
            db.add(User(id=2, display_name="其他资料"))
            await db.flush()
            db.add(Conversation(id=other_id, user_id=2))
            await db.commit()
        for method in ["get", "delete"]:
            response = await getattr(self.client, method)(f"/conversations/{other_id}")
            self.assertEqual(response.status_code, 404)
        self.assertEqual((await self.client.patch(f"/conversations/{other_id}", json={"title": "不可修改"})).status_code, 404)
        self.assertEqual((await self.send(other_id)).status_code, 404)
        listed = (await self.client.get("/conversations")).json()
        self.assertEqual([row["id"] for row in listed], [cid])
        self.assertEqual((await self.client.delete(f"/conversations/{cid}")).status_code, 204)
        self.assertEqual((await self.client.get(f"/conversations/{cid}")).status_code, 404)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(Message)), 0)
            self.assertIsNotNone(await db.get(Conversation, other_id))

    async def test_failure_is_retryable_and_saves_no_half_turn(self):
        """模型失败后释放占用，不保存半轮；用原请求编号可再次发送。"""
        cid = await self.prepare_conversation("记住我叫长青")
        request_id = str(uuid4())
        self.agent.failure = True
        failure = await self.send(cid, request_id=request_id)
        self.assertEqual(failure.status_code, 502, failure.text)
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(saved["messages"], [])
        self.assertFalse(saved["generating"])
        self.agent.failure = False
        self.assertEqual((await self.send(cid, request_id=request_id)).status_code, 200)
        self.assertEqual(len((await self.client.get(f"/conversations/{cid}")).json()["messages"]), 2)

    async def test_concurrent_generation_is_serialized(self):
        """同一会话不同时调用模型，但其他会话可以继续生成。"""
        cid = await self.prepare_conversation()
        slow = RecordingAgent()
        slow.started, slow.release = asyncio.Event(), asyncio.Event()
        self.agent = slow
        task = asyncio.create_task(self.send(cid))
        try:
            await asyncio.wait_for(slow.started.wait(), 20)
            self.assertTrue((await self.client.get(f"/conversations/{cid}")).json()["generating"])
            self.assertEqual((await self.send(cid)).status_code, 409)
            self.assertEqual((await self.client.delete(f"/conversations/{cid}")).status_code, 409)
            self.assertEqual((await self.client.patch(f"/conversations/{cid}", json={"title": "稍后修改"})).status_code, 409)
            self.agent = RecordingAgent()
            other = (await self.client.post("/conversations", json={"first_message": "你好"})).json()["id"]
            self.assertEqual((await self.send(other)).status_code, 200)
        finally:
            slow.release.set()
            result = await task
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(slow.calls), 1)

    async def test_expired_request_cannot_overwrite_new_answer(self):
        """模拟进程失联导致占用过期，恢复后的旧调用不能覆盖新结果。"""
        cid = await self.prepare_conversation()
        slow = RecordingAgent()
        slow.started, slow.release = asyncio.Event(), asyncio.Event()
        self.agent = slow
        task = asyncio.create_task(self.send(cid, "旧问题"))
        try:
            await asyncio.wait_for(slow.started.wait(), 20)
            async with self.sessions() as db:
                conversation = await db.get(Conversation, cid)
                conversation.busy_until = utc_now() - timedelta(seconds=1)
                await db.commit()
            self.agent = RecordingAgent()
            current = await self.send(cid, "新问题")
            self.assertEqual(current.status_code, 200, current.text)
        finally:
            slow.release.set()
            old = await task
        self.assertEqual(old.status_code, 409, old.text)
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual([m["content"] for m in saved["messages"]], ["新问题", "测试回答"])
        self.assertFalse(saved["generating"])

    async def test_cancelled_task_releases_generation(self):
        """真正取消服务端任务时，也应释放生成占用且不保存半轮。"""
        cid = await self.prepare_conversation()
        self.agent.started, self.agent.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.send(cid))
        await asyncio.wait_for(self.agent.started.wait(), 20)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertFalse(saved["generating"])
        self.assertEqual(saved["messages"], [])


if __name__ == "__main__":
    unittest.main()
