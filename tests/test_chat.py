"""验证 LangGraph 对话流程，使用模拟服务，不消耗真实模型额度。"""

import json
import unittest

import httpx
from fastapi import FastAPI
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.graph import build_agent
from app.api.chat import router
from app.api.dependencies import get_agent
from app.core.settings import Settings
from app.llm.errors import ModelError
from app.llm.factory import create_chat_model


def model_response(content):
    """构造符合兼容协议的模拟回答。"""
    return {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }],
    }


def make_settings(host="example.com", model_name="test-model"):
    """测试始终使用假密钥，不读取本地密钥。"""
    return Settings(
        _env_file=None,
        llm_base_url=f"https://{host}/v1/",
        llm_api_key="test-key",
        llm_model=model_name,
    )


class TestAgentGraph(unittest.IsolatedAsyncioTestCase):
    """验证真实图执行、模型切换与调用失败。"""

    async def test_messages_are_appended_and_requests_are_independent(self):
        """图追加模型回答，独立请求之间不会混入对方的消息。"""
        model = FakeMessagesListChatModel(responses=[AIMessage(content="测试回答")])
        agent = build_agent(model)
        for text in ["第一个问题", "另一个问题"]:
            result = await agent.ainvoke({"messages": [("user", text)]})
            self.assertEqual([message.type for message in result["messages"]], ["human", "ai"])
            self.assertEqual(result["messages"][0].text, text)
            self.assertEqual(result["messages"][-1].text, "测试回答")

    async def test_switch_service_by_configuration(self):
        """LangGraph 使用同一套模型客户端切换不同服务。"""
        for host, name in [("bailian.example", "qwen-plus"), ("other.example", "other-model")]:
            with self.subTest(host=host):
                def handle(request):
                    self.assertEqual(str(request.url), f"https://{host}/v1/chat/completions")
                    self.assertEqual(request.headers["Authorization"], "Bearer test-key")
                    payload = json.loads(request.content)
                    self.assertEqual(payload["model"], name)
                    self.assertEqual(payload["messages"], [{"role": "user", "content": "你好"}])
                    return httpx.Response(200, json=model_response("你好！"))

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    agent = build_agent(create_chat_model(make_settings(host, name), client))
                    result = await agent.ainvoke({"messages": [("user", "你好")]})
                    self.assertEqual(result["messages"][-1].text, "你好！")

    async def test_invalid_response(self):
        """空回答和不符合协议的结果应明确报错。"""
        for data in [{}, {"choices": []}, model_response(None), model_response("  ")]:
            with self.subTest(data=data):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data))
                ) as client:
                    agent = build_agent(create_chat_model(make_settings(), client))
                    with self.assertRaises(ModelError):
                        await agent.ainvoke({"messages": [("user", "你好")]})

    async def test_upstream_error_does_not_leak_response(self):
        """错误仅返回状态，不泄露上游响应正文。"""
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, text="secret-upstream-body")
            )
        ) as client:
            agent = build_agent(create_chat_model(make_settings(), client))
            with self.assertRaises(ModelError) as caught:
                await agent.ainvoke({"messages": [("user", "你好")]})
            self.assertIn("401", str(caught.exception))
            self.assertNotIn("secret-upstream-body", str(caught.exception))

    async def test_timeout(self):
        """图节点将模型超时转换成明确的错误。"""
        def handle(request):
            raise httpx.ReadTimeout("模拟超时", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            agent = build_agent(create_chat_model(make_settings(), client))
            with self.assertRaisesRegex(ModelError, "超时"):
                await agent.ainvoke({"messages": [("user", "你好")]})


class TestChatEndpoint(unittest.IsolatedAsyncioTestCase):
    """在 IDEA 中可单独运行接口测试。"""

    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.state.agent = None

    async def test_chat_and_empty_message(self):
        """请求经过真实 LangGraph；空白消息不会执行图。"""
        model = FakeMessagesListChatModel(responses=[AIMessage(content="测试回答")])
        agent = build_agent(model)
        self.app.dependency_overrides[get_agent] = lambda: agent
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        ) as client:
            response = await client.post("/chat", json={"message": "  你好  "})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"reply": "测试回答"})
            response = await client.post("/chat", json={"message": "   "})
            self.assertEqual(response.status_code, 422)

    async def test_model_failure(self):
        """模型服务失败时接口返回 502。"""
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"error": "额度不足"}))
        ) as upstream:
            self.app.state.settings = make_settings()
            self.app.state.http_client = upstream
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                response = await client.post("/chat", json={"message": "你好"})
                self.assertEqual(response.status_code, 502)

    async def test_missing_configuration(self):
        """未配置模型时提供提示，健康检查无需提前连接模型。"""
        self.app.state.settings = Settings(
            _env_file=None, llm_base_url="", llm_api_key="", llm_model=""
        )
        async with httpx.AsyncClient() as upstream:
            self.app.state.http_client = upstream
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                response = await client.post("/chat", json={"message": "你好"})
                self.assertEqual(response.status_code, 503)
                self.assertIn("LLM_API_KEY", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
