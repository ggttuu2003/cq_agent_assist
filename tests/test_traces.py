"""通过真实 MySQL 和模拟模型 HTTP 验证请求记录，不消耗模型额度。"""

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from langchain_core.tools import tool
from sqlalchemy import func, inspect, select, text
from app.db.database import initialize_database

from app.agent.graph import build_agent
from app.api.attachments import router as attachments_router
from app.api.traces import router
from app.core.logging import RequestLoggingMiddleware
from app.db.models import Conversation, User, utc_now
from app.db.traces import AICall, AITrace
from app.dependencies import get_conversation_agent
from app.knowledge.parsing import KnowledgeError
from app.llm.factory import create_chat_model
from app.services.traces import call_context, normalize_request, trace_context
from tests import test_conversations as fixtures
from tests.test_attachments import image_bytes
from tests.test_chat import model_response
from tests.test_streaming import StreamConnection, parse_events
from tests.test_web_search import upstream_stream


class TestRequestNormalization(unittest.TestCase):
    """确认普通正文和工具定义不会在图片归一化时被误改。"""

    def test_only_image_blocks_are_replaced(self):
        image_id = str(uuid4())
        url = 'data:image/png;base64,aGVsbG8='
        digest = hashlib.sha256(url.encode()).hexdigest()
        body = {'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': url}, {'type': 'image_url', 'image_url': {'url': url, 'detail': 'auto'}},
        ]}], 'tools': [{'parameters': {'api_key': {'type': 'string'}}}], 'api_key': 'credential'}
        normalized, images, error = normalize_request(body, {digest: {'id': image_id}})
        self.assertIsNone(error)
        self.assertEqual(normalized['messages'][0]['content'][0]['text'], url)
        self.assertEqual(normalized['messages'][0]['content'][1]['image_url'], {'url': f'attachment://{image_id}', 'detail': 'auto'})
        self.assertEqual(normalized['tools'], body['tools'])
        self.assertEqual(body['api_key'], 'credential')
        self.assertEqual(normalized['api_key'], '[已隐藏]')
        self.assertEqual(len(images), 1)
        _, _, error = normalize_request(body, {})
        self.assertIn('无法恢复', error)


@unittest.skipUnless(os.getenv('CQ_TEST_MYSQL') == '1', '设置 CQ_TEST_MYSQL=1 运行临时 MySQL 调用记录测试')
class TestRequestTraces(unittest.IsolatedAsyncioTestCase):
    """每个测试新建隔离数据库，模型服务只使用 MockTransport。"""

    prepare_conversation = fixtures.TestMySQLConversations.prepare_conversation
    send = fixtures.TestMySQLConversations.send

    async def asyncSetUp(self):
        await fixtures.TestMySQLConversations.asyncSetUp(self)
        self.app.include_router(router)
        self.app.include_router(attachments_router)
        self.app.add_middleware(RequestLoggingMiddleware)
        self.app.dependency_overrides.pop(get_conversation_agent)
        self.app.state.settings = self.app.state.settings.model_copy(update={
            'llm_base_url': 'https://models.example.com/v1', 'search_provider': 'tavily',
        })
        self.failure = False
        self.payloads = []
        self.started = asyncio.Event()
        self.wait_for_model = None
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.app.state.http_client = self.upstream
        self.directory = tempfile.TemporaryDirectory()
        self.storage_patch = patch('app.services.attachments.ATTACHMENT_DIR', Path(self.directory.name))
        self.storage_patch.start()

    async def asyncTearDown(self):
        try:
            await self.upstream.aclose()
            await fixtures.TestMySQLConversations.asyncTearDown(self)
        finally:
            self.storage_patch.stop()
            self.directory.cleanup()

    async def handle(self, request):
        body = json.loads(request.content)
        self.payloads.append(body)
        self.started.set()
        if self.wait_for_model:
            await self.wait_for_model.wait()
        if self.failure:
            return httpx.Response(401, json={'error': {'message': 'private-upstream-body'}})
        if body.get('stream'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=upstream_stream([{'content': '这是回答'}]))
        return httpx.Response(200, json=model_response('这是回答'))

    def trace_path(self, cid, rid):
        return f'/conversations/{cid}/requests/{rid}/trace'

    async def trace(self, cid, rid):
        response = await self.client.get(self.trace_path(cid, rid))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_exact_request_and_replay(self):
        """完整请求与上游收到的数据一致，重放不生成重复记录，也不记录鉴权头。"""
        cid = await self.prepare_conversation()
        rid = str(uuid4())
        first = await self.send(cid, '完整问题', rid)
        self.assertEqual(first.status_code, 200, first.text)
        trace = await self.trace(cid, rid)
        self.assertEqual(len(trace['attempts']), 1)
        attempt = trace['attempts'][0]
        self.assertEqual(attempt['status'], 'completed')
        self.assertIsNone(attempt['retrieval'])
        self.assertFalse(attempt['knowledge_enabled'])
        self.assertEqual(attempt['http_request_id'], first.headers['x-request-id'])
        call = attempt['calls'][0]
        self.assertEqual(call['status'], 'completed')
        self.assertEqual(call['request_body'], self.payloads[0])
        self.assertEqual(call['request_body']['messages'][-1], {'role': 'user', 'content': '完整问题'})
        self.assertIsNone(call['capture_error'])
        self.assertNotIn('authorization', json.dumps(trace).lower())
        raw = await self.client.get(self.trace_path(cid, rid) + f"/calls/{call['id']}/request")
        self.assertEqual(raw.json(), self.payloads[0])
        self.assertEqual((await self.send(cid, '完整问题', rid)).json(), first.json())
        self.assertEqual(len((await self.trace(cid, rid))['attempts']), 1)
        self.assertEqual(len(self.payloads), 1)
        self.assertIsNone(trace_context.get())
        self.assertIsNone(call_context.get())

    async def test_failure_retry_preserves_both_attempts(self):
        """失败时仍保存参数，重试新增独立执行，错误正文不泄漏。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        self.failure = True
        self.assertEqual((await self.send(cid, request_id=rid)).status_code, 502)
        failed = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['calls'][0]['http_status'], 401)
        self.assertEqual(failed['calls'][0]['request_body'], self.payloads[0])
        self.assertNotIn('private-upstream-body', json.dumps(failed))
        self.failure = False
        self.assertEqual((await self.send(cid, request_id=rid)).status_code, 200)
        attempts = (await self.trace(cid, rid))['attempts']
        self.assertEqual([item['status'] for item in attempts], ['failed', 'completed'])
        self.assertNotEqual(attempts[0]['id'], attempts[1]['id'])

    async def test_rag_snapshot_matches_model_system_message(self):
        """实际查询与完整片段保存为快照，并与模型请求内的参考消息一致。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        citation = {'number': 1, 'document_id': str(uuid4()), 'version_id': str(uuid4()),
                    'chunk_id': str(uuid4()), 'title': '学习资料', 'location': '第一段', 'quote': '过拟合的完整解释' * 80}
        self.app.state.knowledge = SimpleNamespace(search=AsyncMock(return_value=[citation]))
        await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        # 本用例验证检索到模型的记录链路，不依赖另一需求的资料有效性查询。
        with patch('app.services.conversations.validate_citations', new=AsyncMock()):
            response = await self.send(cid, '什么是过拟合', rid)
        self.assertEqual(response.status_code, 200, response.text)
        attempt = (await self.trace(cid, rid))['attempts'][0]
        self.assertTrue(attempt['knowledge_enabled'])
        self.assertEqual(attempt['retrieval']['query'], '什么是过拟合')
        self.assertEqual(attempt['retrieval']['citations'], [citation])
        system = attempt['calls'][0]['request_body']['messages'][1]
        self.assertEqual(system['role'], 'system')
        actual_reference = json.loads(system['content'].split('\n', 1)[1])
        self.assertEqual(actual_reference['citations'], attempt['retrieval']['citations'])
        self.assertGreaterEqual(attempt['retrieval']['elapsed_ms'], 0)
        # 后续删除或改名资料不会改写这份历史调用快照。
        citation['quote'] = '改写后的片段'
        self.assertNotEqual((await self.trace(cid, rid))['attempts'][0]['retrieval']['citations'][0]['quote'], citation['quote'])

    async def test_greeting_queries_itself_while_model_retains_chat_history(self):
        """同一会话切到问候后只检索“你好”，聊天模型仍能收到此前的对话历史。"""
        cid = await self.prepare_conversation()
        self.app.state.knowledge = SimpleNamespace(search=AsyncMock(return_value=[]))
        await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        self.assertEqual((await self.send(cid, '什么是过拟合？')).status_code, 200)
        rid = str(uuid4())
        self.assertEqual((await self.send(cid, '你好', rid)).status_code, 200)
        self.app.state.knowledge.search.assert_awaited_with(1, '你好')
        attempt = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(attempt['retrieval']['query'], '你好')
        messages = attempt['calls'][0]['request_body']['messages']
        self.assertEqual([message['content'] for message in messages if message['role'] == 'user'],
                         ['什么是过拟合？', '你好'])
        # 空命中不伪造依据；本轮仍按开关先检索，且记录内容与真正发送的参数一致。
        self.assertEqual(attempt['retrieval']['citations'], [])
        self.assertEqual(attempt['calls'][0]['request_body'], self.payloads[-1])

    async def test_rag_error_and_empty_are_distinct(self):
        """检索失败与无命中都记录真实状态，模型仍可正常回答。"""
        cid = await self.prepare_conversation()
        await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        self.app.state.knowledge = SimpleNamespace(search=AsyncMock(side_effect=KnowledgeError('模拟检索不可用')))
        rid = str(uuid4())
        self.assertEqual((await self.send(cid, request_id=rid)).status_code, 200)
        self.assertEqual((await self.trace(cid, rid))['attempts'][0]['retrieval']['error'], '模拟检索不可用')
        self.app.state.knowledge.search = AsyncMock(return_value=[])
        rid = str(uuid4())
        self.assertEqual((await self.send(cid, request_id=rid)).status_code, 200)
        retrieval = (await self.trace(cid, rid))['attempts'][0]['retrieval']
        self.assertIsNone(retrieval['error'])
        self.assertEqual(retrieval['citations'], [])

    async def test_images_are_references_and_export_restores_exact_bytes(self):
        """图片不重复入库，导出恢复原始请求，文件变化或丢失时明确报错。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        upload = await self.client.post('/attachments', files={'file': ('测试.png', image_bytes(), 'image/png')})
        self.assertEqual(upload.status_code, 201, upload.text)
        image = upload.json()
        response = await self.client.post(f'/conversations/{cid}/messages', json={
            'message': '看图', 'request_id': rid, 'attachment_ids': [image['id']],
        })
        self.assertEqual(response.status_code, 200, response.text)
        call = (await self.trace(cid, rid))['attempts'][0]['calls'][0]
        self.assertEqual(call['images'][0]['attachment'], image)
        self.assertNotIn('base64,', json.dumps(call['request_body']))
        path = self.trace_path(cid, rid) + f"/calls/{call['id']}/request"
        self.assertEqual((await self.client.get(path)).json(), self.payloads[0])
        # 后续问题仍包含历史图片，同样能恢复，并不依赖本轮新上传。
        second_rid = str(uuid4())
        self.assertEqual((await self.send(cid, '图片里还有什么', second_rid)).status_code, 200)
        second_call = (await self.trace(cid, second_rid))['attempts'][0]['calls'][0]
        self.assertEqual(len(second_call['images']), 1)
        self.assertEqual((await self.client.get(self.trace_path(cid, second_rid) + f"/calls/{second_call['id']}/request")).json(), self.payloads[1])
        file = Path(self.directory.name) / image['id']
        file.write_bytes(image_bytes('blue'))
        self.assertEqual((await self.client.get(path)).status_code, 409)
        file.unlink()
        self.assertEqual((await self.client.get(path)).status_code, 409)

    async def test_stream_tool_rounds_have_separate_exact_requests(self):
        """真实流式工具循环的每次调用分别记录，第二次包含工具结果。"""
        @tool
        async def lookup(query: str) -> str:
            """查询测试资料。"""
            return '查到的测试资料'

        requests = []

        async def upstream(request):
            body = json.loads(request.content)
            requests.append(body)
            delta = {'content': '这是回答'} if len(requests) > 1 else {'tool_calls': [{
                'index': 0, 'id': 'lookup-1', 'type': 'function',
                'function': {'name': 'lookup', 'arguments': '{"query":"测试"}'},
            }]}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=upstream_stream([delta]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            model = create_chat_model(self.app.state.settings, client)
            self.app.state.agents['test-model'] = build_agent(model, [lookup]).with_config({'configurable': {}})
            cid, rid = await self.prepare_conversation(), str(uuid4())
            response = await self.client.post(f'/conversations/{cid}/messages/stream', json={'message': '查询', 'request_id': rid})
        self.assertEqual(parse_events(response.text)[-1][0], 'done', response.text)
        calls = (await self.trace(cid, rid))['attempts'][0]['calls']
        self.assertEqual([item['sequence'] for item in calls], [1, 2])
        self.assertEqual([item['request_body'] for item in calls], requests)
        self.assertTrue(all(item['status'] == 'completed' for item in calls))
        self.assertTrue(any(message['role'] == 'tool' for message in calls[1]['request_body']['messages']))
        self.assertEqual(calls[0]['request_body']['tools'][0]['function']['name'], 'lookup')

    async def test_disconnect_keeps_request_and_marks_cancelled(self):
        """等待模型首字时断开，记录仍保留且不长期持有数据库连接。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        self.wait_for_model = asyncio.Event()
        stream = await StreamConnection(self.app, cid, {'message': '慢回答', 'request_id': rid}).start()
        await asyncio.wait_for(self.started.wait(), 5)
        running = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(running['status'], 'running')
        self.assertEqual(running['calls'][0]['request_body'], self.payloads[0])
        self.assertEqual(self.engine.sync_engine.pool.checkedout(), 0)
        await stream.disconnect()
        stopped = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertEqual(stopped['calls'][0]['status'], 'cancelled')
        self.assertEqual(stopped['calls'][0]['request_body'], self.payloads[0])
        saved = (await self.client.get(f'/conversations/{cid}')).json()
        self.assertFalse(saved['generating'])
        self.assertEqual(saved['messages'], [])

    async def test_stop_during_retrieval_preserves_query_without_model_call(self):
        """检索尚未返回时停止也保留查询，不声称已经发送给模型。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        started, release = asyncio.Event(), asyncio.Event()

        async def search(user_id, query):
            started.set()
            await release.wait()
            return []

        self.app.state.knowledge = SimpleNamespace(search=search)
        await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        stream = await StreamConnection(self.app, cid, {'message': '检索途中停止', 'request_id': rid}).start()
        await asyncio.wait_for(started.wait(), 5)
        running = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(running['retrieval']['query'], '检索途中停止')
        self.assertEqual(running['retrieval']['status'], 'running')
        self.assertEqual(running['calls'], [])
        await stream.disconnect()
        stopped = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(stopped['status'], 'cancelled')
        self.assertEqual(stopped['retrieval']['status'], 'cancelled')
        self.assertEqual(stopped['calls'], [])
        self.assertEqual(self.payloads, [])

    async def test_precision_upgrade_preserves_existing_records(self):
        """旧时间精度升级后保留已有请求，重复初始化不改变记录。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        self.assertEqual((await self.send(cid, request_id=rid)).status_code, 200)
        before = await self.trace(cid, rid)
        async with self.engine.begin() as connection:
            await connection.execute(text("ALTER TABLE cq_ai_trace MODIFY created_at DATETIME NOT NULL"))
            await connection.execute(text("ALTER TABLE cq_ai_call MODIFY created_at DATETIME NOT NULL"))
        await initialize_database(self.engine)
        await initialize_database(self.engine)
        after = await self.trace(cid, rid)
        self.assertEqual(before['attempts'][0]['id'], after['attempts'][0]['id'])
        self.assertEqual(before['attempts'][0]['calls'][0]['request_body'], after['attempts'][0]['calls'][0]['request_body'])
        async with self.engine.connect() as connection:
            for table_name in ('cq_ai_trace', 'cq_ai_call'):
                columns = await connection.run_sync(lambda sync, name=table_name: inspect(sync).get_columns(name))
                self.assertEqual(next(item['type'].fsp for item in columns if item['name'] == 'created_at'), 6)

    async def test_isolation_ownership_old_messages_and_delete(self):
        """并发会话不串记录，空请求不造记录，删除会话级联删除调用。"""
        first, second = await self.prepare_conversation(), await self.prepare_conversation()
        r1, r2 = str(uuid4()), str(uuid4())
        results = await asyncio.gather(self.send(first, '第一个问题', r1), self.send(second, '第二个问题', r2))
        self.assertTrue(all(item.status_code == 200 for item in results))
        one, two = await self.trace(first, r1), await self.trace(second, r2)
        self.assertEqual(one['attempts'][0]['calls'][0]['request_body']['messages'][-1]['content'], '第一个问题')
        self.assertEqual(two['attempts'][0]['calls'][0]['request_body']['messages'][-1]['content'], '第二个问题')
        self.assertEqual((await self.trace(first, r2))['attempts'], [])
        missing_call = await self.client.get(self.trace_path(first, r1) + f"/calls/{two['attempts'][0]['calls'][0]['id']}/request")
        self.assertEqual(missing_call.status_code, 404)
        other = str(uuid4())
        async with self.sessions() as db:
            db.add(User(id=2, display_name='其他人'))
            await db.flush()
            db.add(Conversation(id=other, user_id=2))
            await db.commit()
        self.assertEqual((await self.client.get(self.trace_path(other, r1))).status_code, 404)
        self.assertEqual((await self.client.get(self.trace_path(other, r1) + f"/calls/{one['attempts'][0]['calls'][0]['id']}/request")).status_code, 404)
        self.assertEqual((await self.client.delete(f'/conversations/{first}')).status_code, 204)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(AITrace)), 1)
            self.assertEqual(await db.scalar(select(func.count()).select_from(AICall)), 1)

    async def test_recording_failure_does_not_break_chat(self):
        """记录写入失败不能让正常问答报错。"""
        cid = await self.prepare_conversation()
        with patch('app.services.traces.write_record', new=AsyncMock(return_value=False)):
            self.assertEqual((await self.send(cid)).status_code, 200)
        self.assertIsNone(trace_context.get())
        self.assertIsNone(call_context.get())

    async def test_interrupted_trace_and_uncaptured_request(self):
        """重启遗留的过期执行显示中断，不把未捕获请求作为完整JSON导出。"""
        cid, rid = await self.prepare_conversation(), str(uuid4())
        trace_id, call_id = str(uuid4()), str(uuid4())
        async with self.sessions() as db:
            db.add(AITrace(id=trace_id, conversation_id=cid, request_id=rid, http_request_id='-', model='test-model',
                           knowledge_enabled=False, expires_at=utc_now() - timedelta(seconds=1)))
            await db.flush()
            db.add(AICall(id=call_id, trace_id=trace_id, sequence=1, model='test-model'))
            await db.commit()
        attempt = (await self.trace(cid, rid))['attempts'][0]
        self.assertEqual(attempt['status'], 'interrupted')
        self.assertEqual(attempt['calls'][0]['status'], 'interrupted')
        self.assertEqual((await self.client.get(self.trace_path(cid, rid) + f'/calls/{call_id}/request')).status_code, 409)


if __name__ == '__main__':
    unittest.main()
