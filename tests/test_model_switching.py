"""验证模型选择、持久保存与切换和生成并发，不调用真实模型。"""

import asyncio
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from app import dependencies
from app.api.chat import router
from app.db.models import Conversation, User
from app.llm.factory import available_models
from tests import test_conversations as fixtures
from tests.test_chat import make_settings, model_response


class TestModelCatalog(unittest.IsolatedAsyncioTestCase):
    """模型列表无需个人资料或上游模型服务。"""

    async def test_catalog_strips_deduplicates_and_exposes_only_model_info(self):
        """默认模型始终可选；返回能力字段，不返回服务地址或密钥。"""
        app = FastAPI()
        app.include_router(router)
        app.state.settings = make_settings(model_name=" qwen3-vl-plus ").model_copy(update={
            "llm_models": [" qwen-plus ", "", "qwen3-vl-plus", "other-model", "qwen-plus"],
        })
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "default_model": "qwen3-vl-plus",
            "models": [
                {"model": "qwen3-vl-plus", "image_input_supported": True},
                {"model": "qwen-plus", "image_input_supported": False},
                {"model": "other-model", "image_input_supported": None},
            ],
        })
        self.assertNotIn("test-key", response.text)
        self.assertEqual(available_models(make_settings(model_name=" ")), [])


@unittest.skipUnless(os.getenv("CQ_TEST_MYSQL") == "1", "设置 CQ_TEST_MYSQL=1 运行临时 MySQL 数据库测试")
class TestModelSwitching(unittest.IsolatedAsyncioTestCase):
    """每个测试使用独立临时数据库；通过真实接口核对会话与历史模型。"""

    asyncSetUp = fixtures.TestMySQLConversations.asyncSetUp
    asyncTearDown = fixtures.TestMySQLConversations.asyncTearDown
    prepare_conversation = fixtures.TestMySQLConversations.prepare_conversation
    send = fixtures.TestMySQLConversations.send

    def allow_models(self):
        """只调整测试应用的模型列表，不修改本地配置。"""
        self.app.state.settings = self.app.state.settings.model_copy(update={
            "llm_models": ["qwen-plus", "qwen3-vl-plus"],
        })

    async def test_creation_and_switch_preserve_history_and_title(self):
        """创建可选择模型；切换后只影响新回答，标题、旧消息和排序时间保留。"""
        self.allow_models()
        await self.client.get("/profile")
        created = await self.client.post("/conversations", json={
            "first_message": "学习 Python", "model_name": " qwen-plus ",
        })
        self.assertEqual(created.status_code, 201, created.text)
        cid = created.json()["id"]
        self.assertEqual(created.json()["model_name"], "qwen-plus")
        first = (await self.send(cid, "你好")).json()
        switch = await self.client.patch(f"/conversations/{cid}/model", json={"model_name": " qwen3-vl-plus "})
        self.assertEqual(switch.status_code, 200, switch.text)
        self.assertEqual(switch.json(), {"id": cid, "model_name": "qwen3-vl-plus", "image_input_supported": True})
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(saved["title"], first["title"])
        self.assertEqual(saved["updated_at"], first["updated_at"])
        self.assertEqual(saved["messages"], first["messages"])
        second = (await self.send(cid, "继续学习")).json()
        self.assertEqual([row["model"] for row in second["messages"] if row["role"] == "assistant"],
                         ["qwen-plus", "qwen3-vl-plus"])
        self.assertEqual(self.agent.calls[-1][0], ("user", "你好"))
        listed = (await self.client.get("/conversations")).json()
        self.assertEqual(listed[0]["model_name"], "qwen3-vl-plus")

    async def test_invalid_selection_and_ownership(self):
        """不能指定未配置模型、空名或修改别人的会话，失败后原模型不变。"""
        self.allow_models()
        cid = await self.prepare_conversation()
        for model in ["not-configured", " ", "x" * 201]:
            payload = {"model_name": model}
            self.assertEqual((await self.client.patch(f"/conversations/{cid}/model", json=payload)).status_code, 422)
            self.assertEqual((await self.client.post("/conversations", json={"first_message": "你好", **payload})).status_code, 422)
        other = str(uuid4())
        async with self.sessions() as db:
            db.add(User(id=2, display_name="其他资料"))
            await db.flush()
            db.add(Conversation(id=other, user_id=2, model_name="test-model"))
            await db.commit()
        for unknown in [other, str(uuid4())]:
            self.assertEqual((await self.client.patch(f"/conversations/{unknown}/model", json={"model_name": "qwen-plus"})).status_code, 404)
        self.assertEqual((await self.client.get(f"/conversations/{cid}")).json()["model_name"], "test-model")

    async def test_busy_conversation_cannot_switch(self):
        """同一会话生成时返回 409，完成后可以正常切换。"""
        self.allow_models()
        cid = await self.prepare_conversation()
        self.agent.started, self.agent.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.send(cid))
        try:
            await asyncio.wait_for(self.agent.started.wait(), 10)
            response = await self.client.patch(f"/conversations/{cid}/model", json={"model_name": "qwen-plus"})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual((await self.client.get(f"/conversations/{cid}")).json()["model_name"], "test-model")
        finally:
            self.agent.release.set()
            await task
        self.assertEqual((await self.client.patch(f"/conversations/{cid}/model", json={"model_name": "qwen-plus"})).status_code, 200)

    async def test_switch_at_preparation_boundary_uses_consistent_real_graph(self):
        """在准备前切换与调用期间切换，实际请求和记录都匹配领取占用时的模型。"""
        self.allow_models()
        cid = await self.prepare_conversation()
        self.app.dependency_overrides.pop(dependencies.get_conversation_agent)
        self.app.state.settings = self.app.state.settings.model_copy(update={
            "llm_base_url": "https://example.com/v1", "llm_api_key": SecretStr("test-key"),
        })
        original = dependencies.prepare_generation
        calls = []

        async def switch_before_claim(*args):
            response = await self.client.patch(f"/conversations/{cid}/model", json={"model_name": "qwen3-vl-plus"})
            self.assertEqual(response.status_code, 200, response.text)
            return await original(*args)

        async def upstream(request):
            calls.append(json.loads(request.content))
            # 模型已按本次占用选好，其他客户端这时修改模型必须被拒绝。
            response = await self.client.patch(f"/conversations/{cid}/model", json={"model_name": "qwen-plus"})
            self.assertEqual(response.status_code, 409)
            return httpx.Response(200, json=model_response("测试回答"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            self.app.state.http_client = client
            with patch.object(dependencies, "prepare_generation", switch_before_claim):
                response = await self.send(cid)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([call["model"] for call in calls], ["qwen3-vl-plus"])
        self.assertEqual(response.json()["messages"][-1]["model"], "qwen3-vl-plus")
        self.assertFalse(response.json()["generating"])

    async def test_configuration_failure_releases_claim_and_replay_needs_no_model(self):
        """领取后创建客户端失败也释放占用；已保存的重放不依赖模型配置。"""
        cid = await self.prepare_conversation()
        request_id = str(uuid4())
        first = await self.send(cid, request_id=request_id)
        self.app.dependency_overrides.pop(dependencies.get_conversation_agent)
        self.app.state.settings = self.app.state.settings.model_copy(update={"llm_api_key": SecretStr("")})
        async with httpx.AsyncClient() as client:
            self.app.state.http_client = client
            replay = await self.send(cid, request_id=request_id)
            self.assertEqual(replay.json(), first.json())
            failed = await self.send(cid, "新问题")
        self.assertEqual(failed.status_code, 503, failed.text)
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertFalse(saved["generating"])
        self.assertEqual(len(saved["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
