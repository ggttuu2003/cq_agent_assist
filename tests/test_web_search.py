"""验证真实工具循环和搜索适配器，所有网络请求都使用模拟传输，不消耗服务额度。"""

import asyncio
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from langchain_core.messages import ToolMessage
from pydantic import SecretStr, ValidationError

from app.agent.graph import build_agent
from app.dependencies import get_conversation_agent
from app.core.settings import Settings
from app.llm.errors import ModelConfigurationError
from app.llm.factory import create_chat_model
from app.schemas.conversations import SendMessageRequest
from app.search.base import SearchError, SearchResult
from app.search.factory import create_search_provider
from app.search.tavily import TavilySearch
from app.services.conversations import Generation
from app.services.streaming import ConversationStreamResponse
from app.tools.web_search import web_search
from tests.test_chat import make_settings, model_response
from tests.test_streaming import parse_events
from tests import test_streaming as streaming_fixtures


def tool_response(call_id="search-1", query="最新 AI 新闻", content=None):
    """构造模型选择搜索工具的兼容协议响应，允许没有正文。"""
    response = model_response(content)
    response["choices"][0]["finish_reason"] = "tool_calls"
    response["choices"][0]["message"]["tool_calls"] = [{
        "id": call_id, "type": "function", "function": {
            "name": "web_search", "arguments": json.dumps({"query": query}, ensure_ascii=False),
        },
    }]
    return response


class OtherSearch:
    """模拟另一家服务，只实现统一接口，不修改工具或图即可替换 Tavily。"""

    def __init__(self, *, failure=False, empty=False):
        self.calls = []
        self.failure = failure
        self.empty = empty

    async def search(self, query):
        self.calls.append(query)
        if self.failure:
            raise SearchError("模拟搜索服务不可用")
        return [] if self.empty else [SearchResult("真实来源", "https://example.com/news", "搜索摘要")]


class TestSearchProvider(unittest.IsolatedAsyncioTestCase):
    """适配器只输出统一结果，工具不读取服务商字段。"""

    async def test_tavily_request_and_results(self):
        """验证认证、请求格式、摘要限制、去重和无效链接过滤。"""
        def handle(request):
            self.assertEqual(str(request.url), "https://api.tavily.com/search")
            self.assertEqual(request.headers["Authorization"], "Bearer fake-search-key")
            self.assertEqual(json.loads(request.content), {"query": "中文查询", "search_depth": "basic",
                "max_results": 5, "include_answer": False, "include_raw_content": False})
            return httpx.Response(200, json={"results": [
                {"title": "新闻", "url": "https://example.com/a b", "content": "摘要" * 1000},
                {"title": "重复", "url": "https://example.com/a b", "content": "重复内容"},
                {"title": "非法", "url": "javascript:alert(1)", "content": "不能作为来源"},
                {"title": "坏地址", "url": "https://[invalid", "content": "跳过"},
                {"title": "缺少摘要", "url": "https://example.com/other"},
            ]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            results = await TavilySearch("fake-search-key", client).search("中文查询")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.com/a%20b")
        self.assertEqual(len(results[0].content), 1200)

    async def test_errors_do_not_leak_upstream_body(self):
        """认证、限流、超时及异常数据返回中文提示，不泄露响应正文。"""
        for status, expected in [(401, "认证"), (429, "频繁"), (433, "额度"), (500, "不可用")]:
            with self.subTest(status=status):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text="secret-provider-body"),
                )) as client:
                    with self.assertRaisesRegex(SearchError, expected) as caught:
                        await TavilySearch("private-key", client).search("查询")
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertNotIn("private-key", str(caught.exception))
        for data in [{}, {"results": None}, []]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=data),
            )) as client:
                with self.assertRaisesRegex(SearchError, "格式"):
                    await TavilySearch("fake-key", client).search("查询")
        def timeout(request):
            raise httpx.ReadTimeout("内部错误", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaisesRegex(SearchError, "超时"):
                await TavilySearch("fake-key", client).search("查询")

    async def test_config_selects_provider_and_missing_key_disables_search(self):
        """服务由配置选择，空 Key 不启用搜索，不支持的名称明确报错。"""
        async with httpx.AsyncClient() as client:
            settings = Settings(_env_file=None, search_provider="tavily", tavily_api_key="")
            self.assertIsNone(create_search_provider(settings, client))
            configured = settings.model_copy(update={"search_provider": " TAVILY ",
                                                     "tavily_api_key": SecretStr("fake-key")})
            self.assertIsInstance(create_search_provider(configured, client), TavilySearch)
            with self.assertRaises(ModelConfigurationError):
                create_search_provider(settings.model_copy(update={"search_provider": "unknown"}), client)

    async def test_another_provider_uses_the_same_tool(self):
        """替换服务对象，工具名和结构化结果保持不变。"""
        provider = OtherSearch()
        tool = web_search
        result = await tool.ainvoke({"name": "web_search", "args": {"query": "  换一家服务  "},
                                    "id": "other-1", "type": "tool_call"},
                                   config={"configurable": {"search_provider": provider}})
        self.assertEqual(provider.calls, ["换一家服务"])
        self.assertEqual(result.name, "web_search")
        self.assertEqual(result.artifact["sources"][0]["title"], "真实来源")

    async def test_decorated_tool_exposes_only_query_and_isolates_providers(self):
        """装饰器只暴露查询词，不同并发调用分别使用自己的后台服务对象。"""
        self.assertEqual(set(web_search.tool_call_schema.model_json_schema()["properties"]), {"query"})
        first, second = OtherSearch(), OtherSearch()
        await asyncio.gather(
            web_search.ainvoke({"query": "第一个查询"}, config={"configurable": {"search_provider": first}}),
            web_search.ainvoke({"query": "第二个查询"}, config={"configurable": {"search_provider": second}}),
        )
        self.assertEqual(first.calls, ["第一个查询"])
        self.assertEqual(second.calls, ["第二个查询"])

    async def test_tool_parses_args_description_and_validates_query(self):
        """Args 中的中文说明进入模型参数定义，自动推导参数后仍保留查询词校验。"""
        schema = web_search.tool_call_schema.model_json_schema()
        query = schema["properties"]["query"]
        self.assertEqual(query["description"], "需要搜索的关键词或问题。")
        self.assertEqual(query["minLength"], 1)
        self.assertEqual(query["maxLength"], 400)
        self.assertNotIn("Args:", web_search.description)
        provider = OtherSearch()
        for invalid in ["", "   ", "字" * 401]:
            with self.subTest(query=invalid):
                with self.assertRaises(ValidationError):
                    await web_search.ainvoke({"query": invalid}, config={"configurable": {"search_provider": provider}})
        self.assertEqual(provider.calls, [])


class TestSearchGraph(unittest.IsolatedAsyncioTestCase):
    """真实 LangGraph、ToolNode 和模型客户端协作，模拟模型服务决定是否调用搜索。"""

    async def test_tool_selection_context_and_real_source_footer(self):
        """模型可返回空正文的工具调用；结果交回模型，最终回答附真实来源。"""
        calls = []
        provider = OtherSearch()
        def handle(request):
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(200, json=tool_response() if len(calls) == 1 else model_response("整理后的新闻"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            agent = build_agent(create_chat_model(make_settings(), client), [web_search]).with_config(
                {"configurable": {"search_provider": provider}},
            )
            result = await agent.ainvoke({"messages": [("user", "最近有什么 AI 新闻？")]})
        self.assertEqual(provider.calls, ["最新 AI 新闻"])
        self.assertEqual(calls[0]["tools"][0]["function"]["name"], "web_search")
        self.assertEqual([message["role"] for message in calls[1]["messages"]], ["system", "user", "assistant", "tool"])
        self.assertIn("搜索摘要", calls[1]["messages"][-1]["content"])
        self.assertIsInstance(result["messages"][-2], ToolMessage)
        self.assertEqual(result["messages"][-1].text, "整理后的新闻\n\n### 搜索来源\n\n1. [真实来源](<https://example.com/news>)")

    async def test_direct_answer_does_not_search(self):
        """有搜索工具也不强制搜索，普通知识问答可以直接结束。"""
        provider = OtherSearch()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=model_response("异步允许在等待期间处理其他任务")),
        )) as client:
            agent = build_agent(create_chat_model(make_settings(), client), [web_search]).with_config(
                {"configurable": {"search_provider": provider}},
            )
            result = await agent.ainvoke({"messages": [("user", "什么是异步？")]})
        self.assertEqual(provider.calls, [])
        self.assertNotIn("搜索来源", result["messages"][-1].text)

    async def test_search_failure_and_empty_results_are_reported_to_model(self):
        """搜索失败或没有结果时如实返回，不生成虚假的来源链接。"""
        for failure in [True, False]:
            provider = OtherSearch(failure=failure, empty=not failure)
            calls = []
            def handle(request):
                calls.append(json.loads(request.content))
                return httpx.Response(200, json=tool_response() if len(calls) == 1 else model_response("暂时无法获得相关资料"))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                agent = build_agent(create_chat_model(make_settings(), client), [web_search]).with_config(
                    {"configurable": {"search_provider": provider}},
                )
                result = await agent.ainvoke({"messages": [("user", "查询新闻")]})
            data = json.loads(calls[-1]["messages"][-1]["content"])
            self.assertEqual(data["sources"], [])
            self.assertEqual(bool(data["error"]), failure)
            self.assertNotIn("搜索来源", result["messages"][-1].text)

    async def test_search_limit_forces_final_answer(self):
        """三轮工具调用后禁止继续搜索，防止循环消耗额度。"""
        provider, calls = OtherSearch(), []
        def handle(request):
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(200, json=model_response("根据已有结果回答") if body.get("tool_choice") == "none"
                                  else tool_response(call_id=f"search-{len(calls)}"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            agent = build_agent(create_chat_model(make_settings(), client), [web_search]).with_config(
                {"configurable": {"search_provider": provider}},
            )
            result = await agent.ainvoke({"messages": [("user", "继续查资料")]})
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(len(calls), 4)
        self.assertIn("已有结果", result["messages"][-1].text)
        self.assertEqual(result["messages"][-1].text.count("https://example.com/news"), 1)

    async def test_graph_events_reset_intermediate_text_and_keep_final_sources(self):
        """真实图输出搜索开始、结束和最终正文，过渡文字不进入保存内容。"""
        count = 0
        def handle(request):
            nonlocal count
            count += 1
            response = tool_response(content="我先查一下") if count == 1 else model_response("正式回答")
            return httpx.Response(200, json=response)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            # 模拟非流式上游：依然经过真实图的 updates，验证没有片段时也能补齐最终正文。
            agent_model = create_chat_model(make_settings(), client).model_copy(update={"disable_streaming": True})
            agent = build_agent(agent_model, [web_search]).with_config(
                {"configurable": {"search_provider": OtherSearch()}},
            )
            body = SendMessageRequest(message="搜索", request_id=uuid4())
            response = ConversationStreamResponse(None, Generation(1, str(uuid4()), body, 10), agent)
            frames = [frame async for frame in response.graph_events()]
        events = parse_events("".join(frames))
        names = [name for name, _ in events]
        self.assertIn("tool_start", names)
        self.assertIn("tool_end", names)
        self.assertNotIn("我先查一下", response.reply)
        self.assertIn("正式回答", response.reply)
        self.assertIn("https://example.com/news", response.reply)


def upstream_stream(deltas):
    """构造模型真实流式协议的数据，工具参数和文本都可以分多个片段返回。"""
    frames = []
    for delta in deltas:
        payload = {"id": "model-stream", "object": "chat.completion.chunk", "created": 0,
                   "model": "test-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        frames.append("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n")
    return "".join(frames).encode() + b"data: [DONE]\n\n"


def search_call_stream():
    """模型先输出过渡文字，再逐段输出调用参数，模拟百炼兼容接口的工具选择。"""
    return upstream_stream([
        {"content": "我先查一下"},
        {"tool_calls": [{"index": 0, "id": "search-1", "type": "function",
                          "function": {"name": "web_search", "arguments": '{"query": "最新'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": ' AI 新闻"}'}}]},
    ])


@unittest.skipUnless(os.getenv("CQ_TEST_MYSQL") == "1", "设置 CQ_TEST_MYSQL=1 运行临时 MySQL 搜索流式测试")
class TestSearchStreaming(unittest.IsolatedAsyncioTestCase):
    """真实 API、图、模型协议和搜索适配器协作，仅 MySQL 使用隔离临时库。"""

    asyncSetUp = streaming_fixtures.TestStreaming.asyncSetUp
    asyncTearDown = streaming_fixtures.TestStreaming.asyncTearDown
    prepare_conversation = streaming_fixtures.TestStreaming.prepare_conversation

    def configure_search(self, client):
        """让应用的真实依赖创建带搜索工具的图，所有请求仍使用测试网络传输。"""
        self.app.dependency_overrides.pop(get_conversation_agent)
        self.app.state.http_client = client
        self.app.state.settings = self.app.state.settings.model_copy(update={
            "llm_base_url": "https://example.com/v1", "llm_api_key": SecretStr("fake-model-key"),
            "search_provider": "tavily", "tavily_api_key": SecretStr("fake-search-key"),
        })

    async def test_real_search_stream_persistence_and_replay(self):
        """搜索时释放数据库连接；最终文字和来源保存一致，重放不再次搜索或调用模型。"""
        search_started, allow_search, allow_final = asyncio.Event(), asyncio.Event(), asyncio.Event()
        model_calls, search_calls = [], []

        class FinalAnswer(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield upstream_stream([{"content": "根据资料"}]).replace(b"data: [DONE]\n\n", b"")
                await allow_final.wait()
                yield upstream_stream([{"content": "整理的回答"}])

        async def upstream(request):
            if request.url.host == "api.tavily.com":
                search_calls.append(json.loads(request.content))
                search_started.set()
                await allow_search.wait()
                return httpx.Response(200, json={"results": [{"title": "测试新闻", "url": "https://example.com/news", "content": "新闻内容"}]})
            body = json.loads(request.content)
            model_calls.append(body)
            self.assertTrue(body["stream"])
            if len(model_calls) == 1:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=search_call_stream())
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=FinalAnswer())

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            self.configure_search(client)
            cid = await self.prepare_conversation("查询新闻")
            body = {"message": "最近有哪些 AI 新闻？", "request_id": str(uuid4())}
            connection = await streaming_fixtures.StreamConnection(self.app, cid, body).start()
            frames = []
            try:
                while not any(name == "tool_start" for name, _ in parse_events("".join(frames))):
                    frames.append(await connection.next_body())
                await asyncio.wait_for(search_started.wait(), 5)
                self.assertEqual(self.engine.pool.checkedout(), 0)
                self.assertFalse(allow_search.is_set())
                allow_search.set()
                while not any(name == "delta" and data["text"] == "根据资料" for name, data in parse_events("".join(frames))):
                    frames.append(await connection.next_body())
                self.assertFalse(allow_final.is_set())
                allow_final.set()
                await asyncio.wait_for(connection.task, 5)
                while not connection.outgoing.empty():
                    chunk = connection.outgoing.get_nowait().get("body", b"")
                    frames.append(chunk.decode())
            finally:
                await connection.disconnect()
            events = parse_events("".join(frames))
            self.assertIn("answer_reset", [name for name, _ in events])
            end = next(data for name, data in events if name == "tool_end")
            self.assertEqual(end["status"], "completed")
            self.assertEqual(end["sources"], [{"title": "测试新闻", "url": "https://example.com/news"}])
            visible = ""
            for name, data in events:
                if name == "answer_reset":
                    visible = ""
                elif name == "delta":
                    visible += data["text"]
            saved = (await self.client.get(f"/conversations/{cid}")).json()
            self.assertEqual(len(saved["messages"]), 2)
            self.assertEqual(saved["messages"][-1]["content"], visible)
            self.assertNotIn("我先查一下", visible)
            self.assertIn("根据资料整理的回答", visible)
            self.assertIn("https://example.com/news", visible)
            replay = parse_events((await self.client.post(f"/conversations/{cid}/messages/stream", json=body)).text)
            self.assertEqual([name for name, _ in replay], ["delta", "done"])
            self.assertEqual(replay[-1][1]["messages"], saved["messages"])
            self.assertEqual(len(search_calls), 1)
            self.assertEqual(len(model_calls), 2)

    async def test_stop_during_search_cancels_upstream_and_releases_conversation(self):
        """搜索未返回时仍有心跳；停止会取消搜索，不调用下一轮模型或保存半轮问答。"""
        search_started, search_cancelled, never_finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        model_calls = []
        async def upstream(request):
            if request.url.host == "api.tavily.com":
                search_started.set()
                try:
                    await never_finish.wait()
                finally:
                    search_cancelled.set()
            model_calls.append(json.loads(request.content))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=search_call_stream())
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            self.configure_search(client)
            cid = await self.prepare_conversation()
            with patch.object(ConversationStreamResponse, "heartbeat_seconds", 0.02):
                connection = await streaming_fixtures.StreamConnection(self.app, cid,
                    {"message": "搜索后停止", "request_id": str(uuid4())}).start()
                try:
                    await asyncio.wait_for(search_started.wait(), 5)
                    frame = ""
                    while frame != ": ping\n\n":
                        frame = await connection.next_body()
                    self.assertEqual(self.engine.pool.checkedout(), 0)
                finally:
                    await connection.disconnect()
            self.assertTrue(search_cancelled.is_set())
            self.assertEqual(len(model_calls), 1)
            saved = (await self.client.get(f"/conversations/{cid}")).json()
            self.assertFalse(saved["generating"])
            self.assertEqual(saved["messages"], [])


if __name__ == "__main__":
    unittest.main()
