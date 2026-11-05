"""知识资料的解析、持久化任务、检索与引用测试，不消耗真实模型额度。"""

import asyncio
import hashlib
import json
import math
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select

from app.api.knowledge import router
from app.agent.graph import build_agent
from app.agent.retrieval import prepare_retrieval
from app.llm.factory import create_chat_model
from app.db.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeJob, KnowledgeVersion
from app.db.models import User, utc_now
from app.dependencies import get_conversation_agent
from app.knowledge.adapters import MilvusIndex, TextEmbeddings
from app.knowledge.parsing import KnowledgeError, decode_text, split_text
from app.knowledge.worker import KnowledgeWorker, StaleWork
from app.services import knowledge as storage
from app.services.knowledge import KnowledgeService
from app.tools.knowledge_search import CitationContext, knowledge_search
from tests import test_conversations as fixtures
from tests.test_chat import make_settings, model_response
from tests.test_streaming import StreamConnection, parse_events
from tests.test_web_search import upstream_stream


class FakeEmbeddings:
    """确定性向量用于验证数据流，不把它当作生产检索的替代实现。"""
    error = None
    profile = "test-profile"
    collection = "test-collection"
    dimensions = 64

    def __init__(self):
        self.calls, self.failure, self.started, self.release = [], False, None, None

    async def embed(self, texts):
        self.calls.append(texts)
        if self.started:
            self.started.set()
            await self.release.wait()
        if self.failure:
            raise KnowledgeError("模拟向量服务失败")
        vectors = []
        for text in texts:
            values = [0.0] * self.dimensions
            for char in text:
                values[hashlib.sha256(char.encode()).digest()[0] % self.dimensions] += 1
            length = math.sqrt(sum(value * value for value in values)) or 1
            vectors.append([value / length for value in values])
        return vectors


class FakeIndex:
    """在内存保存带归属的向量，单测仍真实经过 MySQL 的版本与归属过滤。"""
    def __init__(self):
        self.rows, self.failure, self.deleted = {}, False, []

    async def ensure(self, collection, dimensions):
        pass

    async def upsert(self, collection, rows):
        self.rows.update({row['id']: row for row in rows})

    async def search(self, collection, user_id, versions, vector, limit):
        rows = [row for row in self.rows.values() if row['user_id'] == user_id and row['version_id'] in versions]
        hits = [{'id': row['id'], 'distance': sum(a * b for a, b in zip(row['vector'], vector))} for row in rows]
        return sorted(hits, key=lambda row: row['distance'], reverse=True)[:limit]

    async def delete_version(self, collection, version_id):
        if self.failure:
            raise KnowledgeError("模拟向量清理失败")
        self.deleted.append(version_id)
        self.rows = {key: value for key, value in self.rows.items() if value['version_id'] != version_id}

    async def close(self):
        pass


class TestKnowledgeText(unittest.IsolatedAsyncioTestCase):
    """格式、分段和向量协议不需要运行数据库。"""

    async def test_decoding_and_offsets(self):
        for encoding in ['utf-8-sig', 'gb18030']:
            text = decode_text('# 配置说明\n\n启动命令是 uvicorn app.main:app\n\n'.encode(encoding))
            self.assertIn('启动命令', text)
        for data in [b'', b' \n ', b'\x00abc', b'%PDF-\x01\x02']:
            with self.assertRaises(KnowledgeError):
                decode_text(data)
        text = '# 项目说明\n\n' + ('启动项目需要 Python 和 MySQL。\n\n' * 90)
        pieces = split_text(text, 300, 50)
        self.assertGreater(len(pieces), 2)
        self.assertEqual(pieces[0].start, 0)
        self.assertEqual(pieces[-1].end, len(text))
        for piece in pieces:
            self.assertEqual(text[piece.start:piece.end], piece.content)
            self.assertLessEqual(len(piece.content), 300)
        for first, second in zip(pieces, pieces[1:]):
            self.assertLess(second.start, first.end)
        with self.assertRaises(KnowledgeError):
            split_text('x', 200, 200)

    async def test_embedding_protocol_and_safe_failure(self):
        settings = make_settings().model_copy(update={'embedding_model': 'text-embedding-v4', 'embedding_dimensions': 64})
        calls = []
        def handle(request):
            body = json.loads(request.content); calls.append(body)
            return httpx.Response(200, json={'object': 'list', 'model': 'text-embedding-v4',
                'data': [{'index': index, 'object': 'embedding', 'embedding': [0.2] * 64} for index in range(len(body['input']))],
                'usage': {'prompt_tokens': 4, 'total_tokens': 4}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = TextEmbeddings(settings, client)
            vectors = await provider.embed(['第一段', '第二段'])
            self.assertEqual(len(vectors), 2)
            self.assertEqual(calls[0]['model'], 'text-embedding-v4')
            self.assertEqual(calls[0]['dimensions'], 64)
            self.assertEqual(calls[0]['input'], ['第一段', '第二段'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401, text='private-key-and-response'))) as client:
            with self.assertRaisesRegex(KnowledgeError, '认证失败') as error:
                await TextEmbeddings(settings, client).embed(['第一段'])
            self.assertNotIn('private', str(error.exception))

    async def test_embedding_batches_obey_service_limit_and_keep_order(self):
        """模拟百炼的 10 条上限，多批次结果必须覆盖全部输入且保持原顺序。"""
        settings = make_settings().model_copy(update={'embedding_model': 'text-embedding-v4', 'embedding_dimensions': 64})
        batches = []

        def handle(request):
            body = json.loads(request.content)
            texts = body['input']
            batches.append(texts)
            if len(texts) > 10:
                return httpx.Response(400, json={'error': {'message': 'batch size must not exceed 10'}})
            return httpx.Response(200, json={'object': 'list', 'model': 'text-embedding-v4',
                'data': [{'index': index, 'object': 'embedding', 'embedding': [float(text)] * 64}
                         for index, text in enumerate(texts)],
                'usage': {'prompt_tokens': len(texts), 'total_tokens': len(texts)}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            vectors = await TextEmbeddings(settings, client).embed([str(index) for index in range(23)])
        self.assertEqual([len(batch) for batch in batches], [10, 10, 3])
        self.assertEqual([vector[0] for vector in vectors], list(range(23)))

    async def test_tool_is_standard_and_contexts_are_independent(self):
        self.assertEqual(set(knowledge_search.tool_call_schema.model_json_schema()['properties']), {'query'})
        first, second = CitationContext(), CitationContext()
        sample = {'chunk_id': 'a', 'number': 99}
        self.assertEqual(first.register([sample, {'chunk_id': 'b'}])[1]['number'], 2)
        self.assertEqual(first.register([sample])[0]['number'], 1)
        self.assertEqual(second.register([{'chunk_id': 'b'}])[0]['number'], 1)
        missing = await knowledge_search.ainvoke({'name': 'knowledge_search', 'args': {'query': '笔记'}, 'type': 'tool_call', 'id': 'k1'}, config={'configurable': {}})
        self.assertIn('尚未准备好', missing.artifact['error'])

    async def test_switch_controls_retrieval_before_model_even_with_long_history(self):
        """模型只直接返回文字，仍必须先检索；关闭后同一张图不能残留上轮来源。"""
        service = AsyncMock()
        citation = {'number': 1, 'document_id': 'd', 'version_id': 'v', 'chunk_id': 'c',
                    'title': 'AI 面试资料', 'location': '第 1 段', 'quote': '缓解过拟合可以使用正则化。'}
        service.search.return_value = [citation]
        calls = []

        def handle(request):
            body = json.loads(request.content)
            calls.append(body)
            if len(calls) == 1:
                service.search.assert_awaited_once()
                self.assertIn('正则化', body['messages'][1]['content'])
            else:
                self.assertNotIn('AI 面试资料', json.dumps(body, ensure_ascii=False))
            return httpx.Response(200, json=model_response('测试回答'))

        history = [('user', '旧问题'), ('assistant', '旧回答')] * 14
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            graph = build_agent(create_chat_model(make_settings(), client))
            result = await graph.ainvoke({'messages': [*history, ('user', '如何缓解过拟合？')]},
                config={'configurable': {'knowledge_enabled': True, 'knowledge_service': service, 'knowledge_user_id': 1}})
            self.assertEqual(result['citations'], [citation])
            result = await graph.ainvoke({'messages': [('user', '如何缓解过拟合？')]},
                config={'configurable': {'knowledge_enabled': False, 'knowledge_service': service, 'knowledge_user_id': 1}})
            self.assertEqual(result['citations'], [])
        service.search.assert_awaited_once()

    async def test_empty_and_failed_retrieval_are_visible_and_model_can_answer(self):
        """空结果和失败均传入模型，但不产生引用；取消则必须终止整轮。"""
        for outcome in [[], KnowledgeError('模拟检索不可用'), asyncio.CancelledError()]:
            service = AsyncMock()
            if isinstance(outcome, BaseException):
                service.search.side_effect = outcome
            else:
                service.search.return_value = outcome
            calls = []

            def handle(request):
                body = json.loads(request.content); calls.append(body)
                expected = '模拟检索不可用' if isinstance(outcome, KnowledgeError) else '未找到相关资料'
                self.assertIn(expected, body['messages'][1]['content'])
                return httpx.Response(200, json=model_response('未获得资料，下面是模型知识补充。'))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                graph = build_agent(create_chat_model(make_settings(), client)).with_config(
                    {'configurable': {'knowledge_enabled': True, 'knowledge_service': service, 'knowledge_user_id': 1}})
                if isinstance(outcome, asyncio.CancelledError):
                    # 框架会把节点自身抛出的取消包装为 NodeCancelledError，仍不得继续调用模型。
                    from langgraph.errors import NodeCancelledError
                    with self.assertRaises(NodeCancelledError):
                        await graph.ainvoke({'messages': [('user', '问题')]})
                    self.assertEqual(calls, [])
                else:
                    result = await graph.ainvoke({'messages': [('user', '问题')]})
                    self.assertEqual(result['citations'], [])

    async def test_followup_query_uses_text_context_without_image_data(self):
        """明确追问补充用户问题；图片和模型回答不进入检索词。"""
        from langchain_core.messages import HumanMessage, AIMessage
        result = prepare_retrieval({'messages': [HumanMessage(content='过拟合和欠拟合有什么区别'),
            AIMessage(content='过拟合可以使用正则化缓解'), HumanMessage(content=[
                {'type': 'text', 'text': '第二种方法呢'},
                {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,secret'}}])]})
        self.assertIn('过拟合', result['retrieval_query'])
        self.assertIn('第二种方法呢', result['retrieval_query'])
        self.assertNotIn('base64', result['retrieval_query'])
        self.assertNotIn('正则化', result['retrieval_query'])
        self.assertLessEqual(len(result['retrieval_query']), 400)

    async def test_independent_questions_do_not_inherit_previous_topic(self):
        """问候、短问题和带完整主题的问题只检索自身，不受长度影响。"""
        from langchain_core.messages import HumanMessage, AIMessage
        history = [HumanMessage(content='什么是过拟合'), AIMessage(content='过拟合可以正则化')]
        for question in ['你好', '谢谢', 'OK', '什么是梯度下降？', '如何缓解过拟合？',
                         '那如何缓解过拟合？', '那么什么是梯度下降？', '推荐 Python 课程',
                         '这是一个新问题：什么是梯度下降？']:
            with self.subTest(question=question):
                result = prepare_retrieval({'messages': [*history, HumanMessage(content=question)]})
                self.assertEqual(result['retrieval_query'], question)

    async def test_explicit_followups_use_user_questions_only(self):
        """省略主题或明确指代的追问可用上文定位，但不拼接工具或模型结果。"""
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
        history = [HumanMessage(content='什么是过拟合'), AIMessage(content='不应进入检索的模型回答'),
                   ToolMessage(content='不应进入检索的网页', tool_call_id='search'),
                   SystemMessage(content='不应进入检索的系统消息')]
        for question in ['那怎么缓解？', '如何缓解？', '它有什么缺点？', '这个有什么用？',
                         '那它怎么实现？', '第二种方法呢？', '上述方法有什么区别？']:
            with self.subTest(question=question):
                query = prepare_retrieval({'messages': [*history, HumanMessage(content=question)]})['retrieval_query']
                self.assertIn('什么是过拟合', query)
                self.assertTrue(query.endswith('当前问题：' + question))
                self.assertNotIn('不应进入检索', query)

    async def test_followups_stop_at_new_topic_and_bound_context(self):
        """连续追问保留当前问题，最近独立问题之前的旧话题不应带入。"""
        from langchain_core.messages import HumanMessage, AIMessage
        questions = ['什么是过拟合', '什么是梯度下降', '它怎么实现？', '那怎么优化？', '它有什么缺点？']
        messages = []
        for question in questions:
            messages.extend([HumanMessage(content=question), AIMessage(content='冗长模型回答' * 100)])
        result = prepare_retrieval({'messages': messages[:-1]})['retrieval_query']
        self.assertIn('什么是梯度下降', result)
        self.assertNotIn('过拟合', result)
        self.assertNotIn('冗长模型回答', result)
        self.assertTrue(result.endswith('当前问题：它有什么缺点？'))
        self.assertLessEqual(len(result), 400)

        long_question = '它有什么缺点？' + '请详细解释每一种情况。' * 40
        self.assertEqual(prepare_retrieval({'messages': [HumanMessage(content='旧话题'),
            HumanMessage(content=long_question)]})['retrieval_query'], long_question[:400])
        near_limit = '它的' + '问题' * 190
        query = prepare_retrieval({'messages': [HumanMessage(content='主题' * 300),
            HumanMessage(content=near_limit)]})['retrieval_query']
        self.assertLessEqual(len(query), 400)
        self.assertTrue(query.endswith(near_limit))
        self.assertEqual(prepare_retrieval({'messages': [HumanMessage(content='那怎么缓解？')]})[
            'retrieval_query'], '那怎么缓解？')


@unittest.skipUnless(os.getenv('CQ_TEST_MYSQL') == '1', '设置 CQ_TEST_MYSQL=1 运行临时 MySQL 知识库测试')
class TestKnowledgePersistence(unittest.IsolatedAsyncioTestCase):
    """每个测试使用临时库和临时文件，模型与向量库使用可控模拟服务。"""
    prepare_conversation = fixtures.TestMySQLConversations.prepare_conversation
    send = fixtures.TestMySQLConversations.send

    async def asyncSetUp(self):
        await fixtures.TestMySQLConversations.asyncSetUp(self)
        self.directory = tempfile.TemporaryDirectory()
        self.storage_patch = patch.object(storage, 'KNOWLEDGE_DIR', Path(self.directory.name))
        self.storage_patch.start()
        self.app.include_router(router)
        self.embedding, self.index = FakeEmbeddings(), FakeIndex()
        self.settings = self.app.state.settings.model_copy(update={'knowledge_chunk_size': 300, 'knowledge_chunk_overlap': 50})
        self.service = KnowledgeService(self.settings, self.sessions, None, embeddings=self.embedding, index=self.index)
        self.app.state.knowledge = self.service
        self.worker = KnowledgeWorker(self.service)
        await self.client.get('/profile')

    async def asyncTearDown(self):
        self.storage_patch.stop(); self.directory.cleanup()
        await fixtures.TestMySQLConversations.asyncTearDown(self)

    async def upload(self, text='项目的启动命令是 python -m uvicorn app.main:app。', name='项目说明.md', replace_id=None):
        path = f'/knowledge/documents/{replace_id}/versions' if replace_id else '/knowledge/documents'
        response = await self.client.post(path, files={'file': (name, text.encode(), 'application/octet-stream')})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    async def drain(self):
        for _ in range(30):
            if not await self.worker.run_once():
                return
        self.fail('后台任务未能结束')

    async def test_upload_dedup_process_search_and_pagination(self):
        queued = await self.upload()
        self.assertEqual(queued['status'], 'queued')
        duplicate = await self.upload(name='另一个名字.txt')
        self.assertEqual(queued['id'], duplicate['id'])
        self.assertEqual(len(list(Path(self.directory.name).iterdir())), 1)
        self.assertEqual(await self.service.search(1, '启动命令'), [])
        await self.drain()
        document = (await self.client.get(f"/knowledge/documents/{queued['id']}")).json()
        self.assertEqual(document['status'], 'ready')
        self.assertGreater(document['chunk_count'], 0)
        result = await self.client.post('/knowledge/retrieve', json={'query': '项目的启动命令'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn('uvicorn', result.json()['citations'][0]['quote'])
        self.assertEqual((await self.client.get('/knowledge')).json()['ready'], 1)
        self.assertIn('uvicorn', (await self.client.get(f"/knowledge/documents/{queued['id']}/content")).json()['content'])
        await self.upload('另一份资料', name='项目说明.md')
        first = (await self.client.get('/knowledge/documents?limit=1')).json()
        second = (await self.client.get('/knowledge/documents', params={'limit': 1, 'cursor': first['next_cursor']})).json()
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertEqual((await self.client.get('/knowledge/documents?cursor=bad!')).status_code, 422)
        self.assertEqual((await self.client.post('/knowledge/documents', files={'file': ('fake.txt', b'\x00bad')})).status_code, 415)
        self.assertEqual((await self.client.post('/knowledge/documents', files={'file': ('fake.pdf', b'%PDF')})).status_code, 415)
        # 同时上传相同内容也只保存一份文件和任务。
        same = await asyncio.gather(self.upload('并发去重内容'), self.upload('并发去重内容'))
        self.assertEqual(same[0]['id'], same[1]['id'])

    async def test_update_failure_keeps_old_version_and_retry_publishes_new(self):
        original = await self.upload(); await self.drain()
        old = (await self.client.get(f"/knowledge/documents/{original['id']}")).json()['active_version_id']
        self.embedding.failure = True
        await self.upload('项目新版启动命令是 python server.py。', replace_id=original['id'])
        self.assertIn('uvicorn', (await self.service.search(1, '项目启动命令'))[0]['quote']) if not self.embedding.failure else None
        await self.drain()
        failure = (await self.client.get(f"/knowledge/documents/{original['id']}")).json()
        self.assertEqual(failure['status'], 'update_failed')
        self.assertEqual(failure['active_version_id'], old)
        self.embedding.failure = False
        self.assertIn('uvicorn', (await self.service.search(1, '项目启动命令'))[0]['quote'])
        retry = await self.client.post(f"/knowledge/documents/{original['id']}/reprocess")
        self.assertEqual(retry.status_code, 202, retry.text)
        await self.drain()
        current = (await self.client.get(f"/knowledge/documents/{original['id']}")).json()
        self.assertNotEqual(current['active_version_id'], old)
        self.assertIn('server.py', (await self.service.search(1, '项目启动命令'))[0]['quote'])
        self.assertIn(old, self.index.deleted)
        self.assertEqual((await self.client.get(f"/knowledge/documents/{original['id']}/content", params={'version_id': old})).status_code, 409)

    async def test_delete_stops_retrieval_and_cleanup_can_retry(self):
        original = await self.upload(); await self.drain()
        self.index.failure = True
        response = await self.client.delete(f"/knowledge/documents/{original['id']}")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(await self.service.search(1, '项目启动命令'), [])
        await self.drain()
        failed = (await self.client.get(f"/knowledge/documents/{original['id']}")).json()
        self.assertEqual(failed['status'], 'cleanup_failed')
        self.assertTrue(list(Path(self.directory.name).iterdir()))
        self.index.failure = False
        await self.client.delete(f"/knowledge/documents/{original['id']}")
        await self.drain()
        self.assertEqual((await self.client.get(f"/knowledge/documents/{original['id']}")).status_code, 404)
        self.assertEqual(self.index.rows, {})
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    async def test_citation_opens_the_correct_page_and_cannot_cross_versions(self):
        """引用第几十段时不只返回第一页，错误的资料和片段组合也不能越权读取。"""
        original = await self.upload('项目说明\n\n' + '这里是很长的项目启动与配置说明。\n\n' * 600)
        await self.drain()
        async with self.sessions() as db:
            fragments = list(await db.scalars(select(KnowledgeChunk).order_by(KnowledgeChunk.position)))
        self.assertGreater(len(fragments), 20)
        target = fragments[-1]
        result = await self.client.get(f"/knowledge/documents/{original['id']}/chunks", params={'chunk_id': target.id})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn(target.id, [item['id'] for item in result.json()['items']])
        result = await self.client.get(f"/knowledge/documents/{original['id']}/chunks", params={'chunk_id': str(uuid4())})
        self.assertEqual(result.status_code, 409)

    async def test_configuration_change_does_not_lose_partial_index_location(self):
        """崩溃恢复时配置变化必须建新版本，不能覆盖旧集合位置导致无法清理。"""
        original = await self.upload()
        async with self.sessions() as db:
            version = await db.get(KnowledgeVersion, original['target_version_id'])
            version.profile, version.collection = 'previous-profile', 'previous-collection'
            await db.commit()
        await self.drain()
        result = (await self.client.get(f"/knowledge/documents/{original['id']}")).json()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('配置已变化', result['error'])
        async with self.sessions() as db:
            version = await db.get(KnowledgeVersion, original['target_version_id'])
            self.assertEqual(version.collection, 'previous-collection')
        await self.client.post(f"/knowledge/documents/{original['id']}/reprocess")
        await self.drain()
        self.assertIn(original['target_version_id'], self.index.deleted)
        self.assertEqual((await self.client.get(f"/knowledge/documents/{original['id']}")).json()['status'], 'ready')

    async def test_deleted_reference_prevents_saving_a_new_answer(self):
        """先检索后删除时，本轮不能保存为有依据的成功回答。"""
        original = await self.upload(); await self.drain()
        citations = await self.service.search(1, '项目启动命令')
        self.assertTrue(citations)
        await self.client.delete(f"/knowledge/documents/{original['id']}")
        from app.services.knowledge import validate_citations
        from fastapi import HTTPException
        async with self.sessions() as db:
            with self.assertRaises(HTTPException) as error:
                await validate_citations(db, 1, citations)
            self.assertEqual(error.exception.status_code, 409)

    async def test_ownership_and_stale_vector_filtering(self):
        original = await self.upload(); await self.drain()
        async with self.sessions() as db:
            db.add(User(id=2, display_name='其他资料'))
            await db.flush()
            (await db.get(KnowledgeDocument, original['id'])).user_id = 2
            await db.commit()
        for path in ['', '/content', '/chunks', '/file']:
            self.assertEqual((await self.client.get(f"/knowledge/documents/{original['id']}{path}")).status_code, 404)
        for method, path in [('post', '/reprocess'), ('delete', '')]:
            self.assertEqual((await getattr(self.client, method)(f"/knowledge/documents/{original['id']}{path}")).status_code, 404)
        self.assertEqual((await self.client.get(f"/knowledge/jobs/{original['job_id']}")).status_code, 404)
        self.assertEqual(await self.service.search(1, '项目启动命令'), [])

    async def test_expired_worker_cannot_publish_and_delete_during_embedding(self):
        original = await self.upload()
        old_work = await self.worker.claim()
        async with self.sessions() as db:
            (await db.get(KnowledgeJob, old_work.id)).lease_until = utc_now() - timedelta(seconds=1)
            await db.commit()
        new_work = await KnowledgeWorker(self.service).claim()
        self.assertEqual(old_work.id, new_work.id)
        self.assertNotEqual(old_work.lease, new_work.lease)
        with self.assertRaises(StaleWork):
            await self.worker.index_document(old_work)
        await self.worker.index_document(new_work)
        await self.upload('删除过程中的新内容', name='将删除.txt')
        self.embedding.started, self.embedding.release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(self.worker.run_once())
        try:
            await asyncio.wait_for(self.embedding.started.wait(), 5)
            self.assertEqual(self.engine.pool.checkedout(), 0)
            rows = (await self.client.get('/knowledge/documents')).json()['items']
            target = next(item for item in rows if item['name'] == '将删除.txt')
            await self.client.delete(f"/knowledge/documents/{target['id']}")
        finally:
            self.embedding.release.set()
            await task
        self.embedding.started = None
        await self.drain()
        self.assertEqual((await self.client.get(f"/knowledge/documents/{target['id']}")).status_code, 404)
        self.assertEqual(len(self.index.rows), 1)

    async def test_real_graph_stream_saves_citations_and_replays_without_search(self):
        original = await self.upload(); await self.drain()
        cid = await self.prepare_conversation('根据我的资料说明启动命令')
        enabled = await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        self.assertEqual(enabled.status_code, 200, enabled.text)
        calls = []
        def upstream(request):
            body = json.loads(request.content); calls.append(body)
            self.assertNotIn('tools', body)
            self.assertIn('项目说明.md', body['messages'][1]['content'])
            self.assertIn('uvicorn', body['messages'][1]['content'])
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=upstream_stream([{'content': '启动命令是 uvicorn。[1] 代码示例：`arr[0]`。'}]))
        self.app.dependency_overrides.pop(get_conversation_agent)
        self.app.state.settings = self.settings.model_copy(update={'llm_base_url': 'https://example.com/v1',
            'llm_api_key': SecretStr('test-key'), 'search_provider': 'tavily', 'tavily_api_key': SecretStr('')})
        body = {'message': '根据我的资料说明启动命令', 'request_id': str(uuid4())}
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            self.app.state.http_client = client
            response = await self.client.post(f'/conversations/{cid}/messages/stream', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            events = parse_events(response.text)
            self.assertIn('retrieval_start', [name for name, _ in events])
            self.assertIn('retrieval_end', [name for name, _ in events])
            self.assertIn('citations', [name for name, _ in events])
            self.assertEqual(events[-1][0], 'done')
            citation = events[-1][1]['messages'][-1]['citations'][0]
            self.assertIn('arr[0]', events[-1][1]['messages'][-1]['content'])
            self.assertEqual(citation['document_id'], original['id'])
            self.assertEqual(citation['number'], 1)
            self.assertEqual(len(calls), 1)
            self.assertLess([name for name, _ in events].index('retrieval_end'), [name for name, _ in events].index('delta'))
            await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': False})
            before = len(self.embedding.calls)
            replay = await self.client.post(f'/conversations/{cid}/messages/stream', json=body)
            self.assertEqual(len(self.embedding.calls), before)
            self.assertEqual(parse_events(replay.text)[-1], events[-1])
        saved = (await self.client.get(f'/conversations/{cid}')).json()
        self.assertEqual(saved['messages'][-1]['citations'][0], citation)
        await self.client.delete(f"/knowledge/documents/{original['id']}"); await self.drain()
        self.assertEqual((await self.client.get(f'/conversations/{cid}')).json()['messages'][-1]['citations'][0], citation)

    async def test_conversation_switch_persists_and_cannot_change_during_generation(self):
        """新旧会话默认关闭，开启值保存在服务端，生成期间不能切换或越权修改。"""
        cid = await self.prepare_conversation()
        self.assertFalse((await self.client.get(f'/conversations/{cid}')).json()['knowledge_enabled'])
        saved = await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        self.assertEqual(saved.json(), {'id': cid, 'knowledge_enabled': True})
        self.assertTrue((await self.client.get(f'/conversations/{cid}')).json()['knowledge_enabled'])
        created = await self.client.post('/conversations', json={'first_message': '新问题', 'knowledge_enabled': True})
        self.assertTrue(created.json()['knowledge_enabled'])
        invalid = await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': 'false'})
        self.assertEqual(invalid.status_code, 422)
        self.agent.started, self.agent.release = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(self.send(cid))
        try:
            await asyncio.wait_for(self.agent.started.wait(), 5)
            blocked = await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': False})
            self.assertEqual(blocked.status_code, 409)
        finally:
            self.agent.release.set()
            await pending
        async with self.sessions() as db:
            from app.db.models import Conversation
            db.add(User(id=2, display_name='其他用户')); await db.flush()
            (await db.get(Conversation, cid)).user_id = 2
            await db.commit()
        self.assertEqual((await self.client.patch(f'/conversations/{cid}/knowledge',
            json={'knowledge_enabled': False})).status_code, 404)

    async def test_stop_during_fixed_retrieval_releases_lease_without_saving(self):
        """检索开始事件先到达，等待期间无数据库连接；停止后不调用模型或保存半轮。"""
        cid = await self.prepare_conversation()
        await self.client.patch(f'/conversations/{cid}/knowledge', json={'knowledge_enabled': True})
        self.app.dependency_overrides.pop(get_conversation_agent)
        self.app.state.settings = self.settings.model_copy(update={'llm_base_url': 'https://example.com/v1',
            'llm_api_key': SecretStr('test-key'), 'search_provider': 'tavily', 'tavily_api_key': SecretStr('')})
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def wait_search(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.fail('停止后不应调用模型'))) as client:
            self.app.state.http_client = client
            with patch.object(self.service, 'search', side_effect=wait_search):
                connection = await StreamConnection(self.app, cid, {'message': '问题', 'request_id': str(uuid4())}).start()
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    event = await connection.next_body()
                    self.assertIn('retrieval_start', event)
                    self.assertEqual(self.engine.pool.checkedout(), 0)
                finally:
                    await connection.disconnect()
                self.assertTrue(cancelled.is_set())
        saved = (await self.client.get(f'/conversations/{cid}')).json()
        self.assertFalse(saved['generating'])
        self.assertEqual(saved['messages'], [])


@unittest.skipUnless(os.getenv('CQ_TEST_MILVUS') == '1', '设置 CQ_TEST_MILVUS=1 使用独立临时 Milvus 集合')
class TestMilvusIntegration(unittest.IsolatedAsyncioTestCase):
    """只创建并删除随机命名的测试集合，不修改应用集合。"""

    async def test_vector_roundtrip_and_version_filter(self):
        from app.core.settings import get_settings
        settings = get_settings()
        collection = 'cq_test_kb_' + uuid4().hex
        index = MilvusIndex(settings)
        try:
            await index.ensure(collection, 64)
            row = {'id': str(uuid4()), 'document_id': str(uuid4()), 'version_id': str(uuid4()), 'user_id': 1,
                   'vector': [1.0] + [0.0] * 63}
            await index.upsert(collection, [row])
            self.assertEqual((await index.search(collection, 1, [row['version_id']], row['vector'], 5))[0]['id'], row['id'])
            self.assertEqual(await index.search(collection, 2, [row['version_id']], row['vector'], 5), [])
            await index.delete_version(collection, row['version_id'])
            self.assertEqual(await index.search(collection, 1, [row['version_id']], row['vector'], 5), [])
        finally:
            if index.client:
                await index.client.drop_collection(collection)
            await index.close()


if __name__ == '__main__':
    unittest.main()
