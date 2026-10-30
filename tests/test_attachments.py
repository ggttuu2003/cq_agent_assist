"""验证图片上传、模型输入、历史追问与流式重试，真实模型请求使用模拟服务。"""

import asyncio
import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException, UploadFile
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.agent.graph import build_agent
from app.api.attachments import router
from app.db.models import Attachment, Conversation, Message, MessageAttachment, User
from app.dependencies import get_conversation_agent
from app.llm.errors import ModelError
from app.llm.factory import create_chat_model, image_input_support
from app.schemas.conversations import SendMessageRequest
from app.services.attachments import upload_image, validate_image
from app.services.conversations import model_history
from tests import test_conversations as fixtures
from tests.test_chat import make_settings, model_response
from tests.test_streaming import StreamConnection, StreamingAgent, parse_events


def image_bytes(color="red", image_format="PNG"):
    """生成尺寸很小的有效图片，不依赖网络素材。"""
    output = io.BytesIO()
    Image.new("RGB", (24, 16), color).save(output, format=image_format)
    return output.getvalue()


class TestImageValidation(unittest.IsolatedAsyncioTestCase):
    """图片真实性检查及模型协议转换，不需要 MySQL。"""

    async def test_known_text_model_cannot_silently_ignore_image(self):
        """纯文本模型在发送网络请求前报错，未知模型保留扩展空间。"""
        self.assertIs(image_input_support("qwen-plus"), False)
        self.assertIs(image_input_support("qwen3-vl-plus"), True)
        self.assertIsNone(image_input_support("other-model"))
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx.Response(200, json=model_response("我没看到图片"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            agent = build_agent(create_chat_model(make_settings(model_name="qwen-plus"), client))
            with self.assertRaisesRegex(ModelError, "仅支持文字"):
                await agent.ainvoke({"messages": [("user", [
                    {"type": "text", "text": "解释截图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_bytes()).decode()}},
                ])]})
        self.assertEqual(calls, [])

    async def test_image_only_request_has_server_side_default_question(self):
        """各客户端只发送附件也能提问，没有文字和附件时仍拒绝空消息。"""
        for data in [{"message": "  "}, {}]:
            request = SendMessageRequest(request_id=uuid4(), attachment_ids=[uuid4()], **data)
            self.assertEqual(request.message, "请描述这些图片")
        with self.assertRaises(ValidationError):
            SendMessageRequest(message="  ", request_id=uuid4())

    async def test_actual_format_and_invalid_files(self):
        """真实图片可识别，普通文件、SVG 和损坏文件不会被当作图片。"""
        for image_format, expected in [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]:
            self.assertEqual(validate_image(image_bytes(image_format=image_format)), expected)
        for data in [b"not-an-image", b"<svg><script>alert(1)</script></svg>", image_bytes()[:30]]:
            with self.assertRaises(HTTPException) as caught:
                validate_image(data)
            self.assertEqual(caught.exception.status_code, 415)

    async def test_animated_and_oversized_images_are_rejected(self):
        """静态图片限制同时适用于动图帧数和像素数量。"""
        animated = io.BytesIO()
        Image.new("RGB", (24, 16), "red").save(animated, format="PNG", save_all=True,
            append_images=[Image.new("RGB", (24, 16), "blue")])
        with self.assertRaises(HTTPException) as caught:
            validate_image(animated.getvalue())
        self.assertEqual(caught.exception.status_code, 415)
        large = io.BytesIO()
        Image.new("1", (4001, 4000)).save(large, format="PNG")
        with self.assertRaises(HTTPException) as caught:
            validate_image(large.getvalue())
        self.assertEqual(caught.exception.status_code, 413)

    async def test_failed_database_save_removes_file(self):
        """文件已经写入但记录提交失败时，不遗留无法查询的文件。"""
        with tempfile.TemporaryDirectory() as directory, patch("app.services.attachments.ATTACHMENT_DIR", Path(directory)):
            db = AsyncMock()
            db.add = Mock()
            db.commit.side_effect = SQLAlchemyError("模拟提交失败")
            with self.assertRaises(SQLAlchemyError):
                await upload_image(db, 1, UploadFile(file=io.BytesIO(image_bytes()), filename="图片.png"))
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_model_history_limits_images_by_complete_rounds(self):
        """计入当前图片后按整轮裁剪历史，不把较早的问题或图片单独遗留。"""
        with tempfile.TemporaryDirectory() as directory, patch("app.services.attachments.ATTACHMENT_DIR", Path(directory)):
            aid = str(uuid4())
            data = image_bytes()
            Path(directory, aid).write_bytes(data)
            attachment = Attachment(id=aid, name="图.png", media_type="image/png", size=len(data))
            rows = [Message(role="user", content="看这张图", attachment_links=[MessageAttachment(position=0, attachment=attachment)]),
                    Message(role="assistant", content="一张红色图片", attachment_links=[])]
            history = model_history(rows)
            self.assertEqual(base64.b64decode(history[0][1][1]["image_url"]["url"].split(",", 1)[1]), data)
            self.assertEqual(model_history(rows, image_count=4), [])
            self.assertEqual(model_history(rows, image_size=10 * 1024 * 1024), [])

    async def test_image_reaches_actual_model_protocol_and_errors_are_safe(self):
        """真实 LangGraph 和 LangChain 将内容块传入模型协议，错误不泄露上游正文。"""
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes()).decode()
        content = [{"type": "text", "text": "图片是什么颜色？"}, {"type": "image_url", "image_url": {"url": data_url}}]
        calls = []

        def upstream(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=model_response("红色"))

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            agent = build_agent(create_chat_model(make_settings(model_name="qwen3-vl-plus"), client))
            result = await agent.ainvoke({"messages": [("user", content)]})
        self.assertEqual(calls[0]["messages"][-1]["content"], content)
        self.assertEqual(result["messages"][-1].text, "红色")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(400, text="private-image-response"))) as client:
            agent = build_agent(create_chat_model(make_settings(), client))
            with self.assertRaisesRegex(ModelError, "本会话模型支持图片") as caught:
                await agent.ainvoke({"messages": [("user", content)]})
            self.assertNotIn("private", str(caught.exception))


@unittest.skipUnless(os.getenv("CQ_TEST_MYSQL") == "1", "设置 CQ_TEST_MYSQL=1 运行隔离 MySQL 图片测试")
class TestImageConversations(unittest.IsolatedAsyncioTestCase):
    """附件、消息和会话都使用隔离数据库，图片放在自动清理的临时目录。"""

    prepare_conversation = fixtures.TestMySQLConversations.prepare_conversation

    async def asyncSetUp(self):
        await fixtures.TestMySQLConversations.asyncSetUp(self)
        self.app.include_router(router)
        self.directory = tempfile.TemporaryDirectory()
        self.storage_patch = patch("app.services.attachments.ATTACHMENT_DIR", Path(self.directory.name))
        self.storage_patch.start()

    async def asyncTearDown(self):
        try:
            await fixtures.TestMySQLConversations.asyncTearDown(self)
        finally:
            self.storage_patch.stop()
            self.directory.cleanup()

    async def upload(self, color="red"):
        """经过真实 multipart 上传接口获得附件资料。"""
        response = await self.client.post("/attachments", files={"file": ("../图片.jpg", image_bytes(color), "text/plain")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_upload_read_limits_and_ownership(self):
        """后端按真实内容识别类型，并阻止无效文件、过大文件和跨用户访问。"""
        await self.client.get("/profile")
        attachment = await self.upload()
        self.assertEqual(attachment["name"], "图片.jpg")
        self.assertEqual(attachment["media_type"], "image/png")
        response = await self.client.get(attachment["url"])
        self.assertEqual(response.content, image_bytes())
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-type"], "image/png")
        invalid = await self.client.post("/attachments", files={"file": ("fake.png", b"not image", "image/png")})
        self.assertEqual(invalid.status_code, 415)
        too_big = await self.client.post("/attachments", files={"file": ("big.png", b"x" * (5 * 1024 * 1024 + 1), "image/png")})
        self.assertEqual(too_big.status_code, 413)
        empty = await self.client.post("/attachments", files={"file": ("empty.png", b"", "image/png")})
        self.assertEqual(empty.status_code, 422)
        other_id = str(uuid4())
        async with self.sessions() as db:
            db.add(User(id=2, display_name="另一份资料"))
            await db.flush()
            db.add(Attachment(id=other_id, user_id=2, name="其他.png", media_type="image/png", size=10))
            await db.commit()
        self.assertEqual((await self.client.get(f"/attachments/{other_id}")).status_code, 404)

    async def test_image_order_persistence_followup_replay_and_delete(self):
        """多图顺序在保存、刷新和追问中保持一致，改图或换顺序不能复用旧请求编号。"""
        cid = await self.prepare_conversation("比较两张图片")
        red, blue = await self.upload(), await self.upload("blue")
        ids = [blue["id"], red["id"]]
        body = {"message": "比较这两张图片", "request_id": str(uuid4()), "attachment_ids": ids}
        path = f"/conversations/{cid}/messages"
        response = await self.client.post(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()["messages"][0]["attachments"]], ids)
        content = self.agent.calls[0][-1][1]
        self.assertEqual(base64.b64decode(content[1]["image_url"]["url"].split(",")[1]), image_bytes("blue"))
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual([item["id"] for item in saved["messages"][0]["attachments"]], ids)
        self.assertEqual((await self.client.get("/conversations")).json()[0]["messages"][0]["attachments"], saved["messages"][0]["attachments"])
        replay = await self.client.post(path, json=body)
        self.assertEqual(replay.json(), saved)
        self.assertEqual(len(self.agent.calls), 1)
        for changed in [[red["id"]], ids[::-1]]:
            self.assertEqual((await self.client.post(path, json={**body, "attachment_ids": changed})).status_code, 409)
        followup = await self.client.post(path, json={"message": "第一张是什么颜色", "request_id": str(uuid4())})
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(self.agent.calls[-1][0][1], content)
        self.assertEqual((await self.client.delete(f"/conversations/{cid}")).status_code, 204)
        async with self.sessions() as db:
            self.assertEqual(await db.scalar(select(func.count()).select_from(MessageAttachment)), 0)
        self.assertEqual((await self.client.get(red["url"])).status_code, 200)

    async def test_missing_or_invalid_attachments_do_not_leave_lease(self):
        """图片文件丢失或输入不合法时，不调用模型，也不会让会话永久停在生成中。"""
        cid = await self.prepare_conversation()
        attachment = await self.upload()
        path = f"/conversations/{cid}/messages"
        base = {"message": "看图", "request_id": str(uuid4())}
        self.assertEqual((await self.client.post(path, json={**base, "attachment_ids": [attachment["id"]] * 2})).status_code, 422)
        self.assertEqual((await self.client.post(path, json={**base, "attachment_ids": [str(uuid4())]})).status_code, 404)
        Path(self.directory.name, attachment["id"]).unlink()
        self.assertEqual((await self.client.post(path, json={**base, "attachment_ids": [attachment["id"]]})).status_code, 409)
        async with self.sessions() as db:
            conversation = await db.get(Conversation, cid)
            self.assertIsNone(conversation.active_request)
            self.assertIsNone(conversation.busy_until)
        self.assertEqual(self.agent.calls, [])
        self.assertEqual((await self.client.get(attachment["url"])).status_code, 404)

    async def test_old_text_conversation_rejects_images_before_stream_starts(self):
        """旧会话绑定纯文本模型时，两个发送接口都提前返回明确错误且不保存错误问答。"""
        cid = await self.prepare_conversation()
        attachment = await self.upload()
        async with self.sessions() as db:
            (await db.get(Conversation, cid)).model_name = "qwen-plus"
            await db.commit()
        info = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertIs(info["image_input_supported"], False)
        body = {"message": "解释截图", "request_id": str(uuid4()), "attachment_ids": [attachment["id"]]}
        for ending in ["messages", "messages/stream"]:
            response = await self.client.post(f"/conversations/{cid}/{ending}", json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("qwen-plus", response.json()["detail"])
            self.assertIn("切换", response.json()["detail"])
        self.assertEqual(self.agent.calls, [])
        async with self.sessions() as db:
            self.assertIsNone((await db.get(Conversation, cid)).active_request)
            self.assertEqual(await db.scalar(select(func.count()).select_from(Message)), 0)

    async def test_old_text_conversation_keeps_chatting_without_history_images(self):
        """历史里已有误发图片时，仍可继续普通聊天，图片保持可展示而不再传给纯文本模型。"""
        cid = await self.prepare_conversation()
        attachment = await self.upload()
        path = f"/conversations/{cid}/messages"
        result = await self.client.post(path, json={"message": "解释截图", "request_id": str(uuid4()),
                                                   "attachment_ids": [attachment["id"]]})
        self.assertEqual(result.status_code, 200, result.text)
        async with self.sessions() as db:
            (await db.get(Conversation, cid)).model_name = "qwen-plus"
            await db.commit()
        result = await self.client.post(path, json={"message": "介绍一下 Python", "request_id": str(uuid4())})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.agent.calls[-1][0], ("user", "解释截图"))
        self.assertEqual(result.json()["messages"][0]["attachments"][0]["id"], attachment["id"])

    async def test_switch_existing_text_conversation_to_vision_and_back(self):
        """旧会话切换后可原地发图，切回文字模型只传历史文字，再切回仍可追问图片。"""
        self.app.state.settings = self.app.state.settings.model_copy(update={
            "llm_model": "qwen-plus", "llm_models": ["qwen-plus", "qwen3-vl-plus"],
        })
        cid = await self.prepare_conversation()
        attachment = await self.upload()
        model_path = f"/conversations/{cid}/model"
        message_path = f"/conversations/{cid}/messages"
        self.assertEqual((await self.client.patch(model_path, json={"model_name": "qwen3-vl-plus"})).status_code, 200)
        answer = await self.client.post(message_path, json={"message": "解释截图", "request_id": str(uuid4()),
                                                           "attachment_ids": [attachment["id"]]})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIsInstance(self.agent.calls[-1][-1][1], list)
        self.assertEqual(answer.json()["messages"][-1]["model"], "qwen3-vl-plus")
        self.assertEqual((await self.client.patch(model_path, json={"model_name": "qwen-plus"})).status_code, 200)
        answer = await self.client.post(message_path, json={"message": "继续聊文字", "request_id": str(uuid4())})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self.agent.calls[-1][0], ("user", "解释截图"))
        self.assertEqual(answer.json()["messages"][0]["attachments"][0]["id"], attachment["id"])
        self.assertEqual((await self.client.patch(model_path, json={"model_name": "qwen3-vl-plus"})).status_code, 200)
        answer = await self.client.post(message_path, json={"message": "截图是什么颜色", "request_id": str(uuid4())})
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(self.agent.calls[-1][0][1][1]["type"], "image_url")
        self.assertEqual([item["model"] for item in answer.json()["messages"] if item["role"] == "assistant"],
                         ["qwen3-vl-plus", "qwen-plus", "qwen3-vl-plus"])

    async def test_image_total_size_limit_precedes_model_execution(self):
        """多张合法附件的总大小仍需要校验，不能绕过单张上传限制。"""
        cid = await self.prepare_conversation()
        images = [await self.upload() for _ in range(3)]
        async with self.sessions() as db:
            for item in images:
                (await db.get(Attachment, item["id"])).size = 4 * 1024 * 1024
            await db.commit()
        response = await self.client.post(f"/conversations/{cid}/messages", json={
            "message": "比较图片", "request_id": str(uuid4()), "attachment_ids": [item["id"] for item in images],
        })
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.agent.calls, [])

    async def test_stream_stop_retry_and_done_keep_original_image(self):
        """首字前停止不保存半轮，重试复用原图和请求编号，最终 done 与历史一致。"""
        cid = await self.prepare_conversation()
        attachment = await self.upload()
        self.agent = StreamingAgent()
        self.agent.allow_first = asyncio.Event()
        body = {"message": "描述图片", "request_id": str(uuid4()), "attachment_ids": [attachment["id"]]}
        connection = await StreamConnection(self.app, cid, body).start()
        await asyncio.wait_for(self.agent.started.wait(), 3)
        await connection.disconnect()
        saved = (await self.client.get(f"/conversations/{cid}")).json()
        self.assertEqual(saved["messages"], [])
        self.assertFalse(saved["generating"])
        self.agent.allow_first.set()
        response = await self.client.post(f"/conversations/{cid}/messages/stream", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        events = parse_events(response.text)
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["messages"][0]["attachments"][0]["id"], attachment["id"])
        self.assertEqual(self.agent.calls[0][-1], self.agent.calls[1][-1])
        replay = await self.client.post(f"/conversations/{cid}/messages/stream", json=body)
        self.assertEqual(parse_events(replay.text)[-1][1]["messages"], events[-1][1]["messages"])
        self.assertEqual(len(self.agent.calls), 2)
        self.assertNotIn("base64", response.text)


if __name__ == "__main__":
    unittest.main()
