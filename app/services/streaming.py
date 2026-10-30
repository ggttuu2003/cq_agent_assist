"""把会话生成结果转换为 SSE，并管理连接断开后的取消与清理。"""

import asyncio
import json
import logging
from contextlib import aclosing
from time import perf_counter

import anyio
from fastapi import HTTPException
from langchain_core.messages import AIMessageChunk, ToolMessage
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.llm.errors import ModelError
from app.services.conversations import Generation, release_generation, save_generation

logger = logging.getLogger(__name__)


def sse_event(name: str, data: dict) -> str:
    """JSON 转义正文里的换行，使用空行分隔事件。"""
    # event 是事件名，data 是 JSON 正文；末尾两个换行表示一个事件结束。
    # ensure_ascii=False 保留中文，正文中的换行由 JSON 转义，避免被误认为事件边界。
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class ConversationStreamResponse(StreamingResponse):
    """流式会话响应：负责心跳、生成完成、错误和客户端断开的整个生命周期。"""

    # 主要调用顺序：框架调用 __call__ → send_events → 父类 stream_response
    # → events → model_events → save_reply，结束或断开后统一清理。
    heartbeat_seconds = 15
    media_type = "text/event-stream"

    def __init__(self, sessions, generation: Generation, agent):
        """保存会话工厂和本次生成数据，不保存请求依赖中的数据库连接。"""
        # sessions 是创建 AsyncSession 的工厂；需要执行 SQL 时再用 self.sessions() 创建 db。
        # generation 是问题、历史消息、占用编号等普通数据，agent 是已编译的 LangGraph。
        self.sessions = sessions
        self.generation = generation
        self.agent = agent
        self.started_at = perf_counter()
        self.first_text_received = False
        self.failed = False
        # 只累积当前模型回答；模型转去调用工具时清空，最终不会保存过渡文字。
        self.reply = ""
        # completed 表示问答已保存成功，不表示“已经输出过文字”。
        # 重试时若查到了已保存问答，直接重放结果，所以初始值也可能为 True。
        self.completed = generation.saved is not None
        # events 内部使用 yield，调用它只创建异步生成器，此时不会开始调用模型。
        # 父类把这个生成器保存为 body_iterator，发送响应时再逐段读取。
        # 两个响应头分别禁止缓存、提示支持该响应头的代理不要缓冲回答。
        super().__init__(self.events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no",
        })

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """同时监听断开和发送数据，等待第一个片段时也能响应停止。"""
        # 框架通过 await response(scope, receive, send) 进入这里。
        # scope 是请求信息；receive 接收断开等事件；send 用于发送响应头和正文。
        logger.info("流式回答开始 会话=%s 消息请求=%s 模型=%s 重放=%s",
                    self.generation.conversation_id, self.generation.body.request_id,
                    self.generation.model_name, self.completed)
        try:
            async with anyio.create_task_group() as group:
                # 启动发送任务，与下面的断开监听并发执行，不是创建两条线程。
                group.start_soon(self.send_events, send, group.cancel_scope)
                # 父类方法会一直等到客户端断开；模型还没返回首字时也能监听。
                await self.listen_for_disconnect(receive)
                # 客户端先断开时，取消同组的发送任务；发送先完成则由 send_events 取消监听。
                group.cancel_scope.cancel()
        finally:
            # shield=True 保护的是清理过程，防止外层取消再次打断清理，不会让模型继续生成。
            with anyio.CancelScope(shield=True):
                try:
                    # 关闭 events 生成器，触发内部关闭模型流或取消保存任务的 finally。
                    await self.body_iterator.aclose()
                finally:
                    # 清理必须独立执行：即使关闭生成器失败，也要尝试释放本次占用。
                    # 正常保存已经释放占用；未完成时按 lease 核对，不能清掉后续请求的占用。
                    if not self.completed and self.generation.lease:
                        try:
                            # 这里只限制释放占用最多等待 10 秒，不是模型生成的总时限。
                            with anyio.fail_after(10):
                                async with self.sessions() as db:
                                    await release_generation(db, self.generation.user_id,
                                                             self.generation.conversation_id,
                                                             self.generation.lease)
                        except Exception:
                            logger.exception("流式生成占用清理失败，将由占用到期机制恢复 会话=%s 消息请求=%s",
                                             self.generation.conversation_id, self.generation.body.request_id)
                    outcome = "已保存" if self.completed else "失败" if self.failed else "取消或连接断开"
                    logger.info("流式回答结束 会话=%s 消息请求=%s 结果=%s 耗时=%.0fms",
                                self.generation.conversation_id, self.generation.body.request_id,
                                outcome, (perf_counter() - self.started_at) * 1000)

    async def send_events(self, send: Send, cancel_scope: anyio.CancelScope):
        """发送结束或浏览器断开后，让另一个监听任务一同退出。"""
        try:
            # 父类负责发送响应头，并通过 async for 读取 body_iterator，把每一段写给浏览器。
            await self.stream_response(send)
        except OSError:
            # 有些 ASGI 服务通过发送异常通知断开，同样进入统一清理。
            pass
        finally:
            # 发送正常结束或失败后，另一个任务都无需继续等待客户端断开。
            cancel_scope.cancel()

    async def graph_events(self):
        """把模型片段和节点结果转换成页面事件，工具名称与搜索服务商无关。"""
        generation = self.generation
        request_id = str(generation.body.request_id)
        calls = {}
        final_received = False
        # messages 提供实时文字；updates 提供节点完成后的模型消息或工具执行结果。
        # 同时读取两种模式，才能区分“模型选择工具”和“模型给出最终回答”。
        async with aclosing(self.agent.astream({
            "messages": [*generation.history, ("user", generation.current_content or generation.body.message)],
        }, stream_mode=["messages", "updates"])) as events:
            async for mode, data in events:
                if mode == "messages":
                    message, metadata = data
                    if metadata.get("langgraph_node") == "model" and isinstance(message, AIMessageChunk):
                        if message.text:
                            if not self.first_text_received:
                                self.first_text_received = True
                                logger.info("收到模型首个文本片段 会话=%s 消息请求=%s 等待=%.0fms",
                                            generation.conversation_id, generation.body.request_id,
                                            (perf_counter() - self.started_at) * 1000)
                            self.reply += message.text
                            yield sse_event("delta", {"request_id": request_id, "text": message.text})
                    continue

                model_result = data.get("model")
                if model_result:
                    response = model_result["messages"][-1]
                    if response.tool_calls:
                        # 模型可能先说“我来查一下”再提出工具调用，这部分不是最终答案。
                        if self.reply:
                            self.reply = ""
                            yield sse_event("answer_reset", {"request_id": request_id})
                        for call in response.tool_calls:
                            query = str(call["args"].get("query", ""))[:400]
                            calls[call["id"]] = query
                            yield sse_event("tool_start", {"request_id": request_id,
                                "call_id": call["id"], "tool": call["name"], "query": query})
                    else:
                        # 模型节点会在最终正文后追加真实来源；只补发尚未流式输出的部分。
                        text = response.text
                        if text.startswith(self.reply):
                            remaining = text[len(self.reply):]
                        else:
                            remaining = text
                            yield sse_event("answer_reset", {"request_id": request_id})
                        self.reply = text
                        final_received = True
                        if remaining:
                            yield sse_event("delta", {"request_id": request_id, "text": remaining})

                tools_result = data.get("tools")
                if tools_result:
                    for message in tools_result["messages"]:
                        if not isinstance(message, ToolMessage):
                            continue
                        artifact = message.artifact if isinstance(message.artifact, dict) else {}
                        error = artifact.get("error")
                        if message.status == "error":
                            error = "搜索工具调用失败，请检查查询参数后重试"
                        yield sse_event("tool_end", {"request_id": request_id,
                            "call_id": message.tool_call_id, "tool": message.name,
                            "query": calls.get(message.tool_call_id, ""),
                            "status": "error" if error else "completed", "error": error,
                            "sources": [{"title": item["title"], "url": item["url"]}
                                        for item in artifact.get("sources", [])]})
        if not final_received:
            raise ModelError("模型未生成最终回答，请重试")

    async def model_events(self):
        """读取真实模型片段；等待时发送心跳，结束时关闭图迭代器。"""
        generation = self.generation
        iterator = self.graph_events()
        next_chunk = None
        try:
            # 这是生成阶段的总时限；心跳只能保持连接活跃，不能延长这个时限。
            async with asyncio.timeout(generation.duration):
                while True:
                    # anext 获取下一段；单独创建任务，才能在等模型时穿插发送心跳。
                    next_chunk = asyncio.create_task(anext(iterator))
                    # asyncio.wait 返回 (已完成任务集合, 未完成任务集合)，[0] 取前者。
                    # 15 秒后集合仍为空就发送心跳；wait 超时不会取消任务，继续等同一个片段。
                    while not (await asyncio.wait({next_chunk}, timeout=self.heartbeat_seconds))[0]:
                        # 冒号开头是 SSE 注释，前端忽略它，不会显示在回答中。
                        yield ": ping\n\n"
                    try:
                        # 任务完成后取结果；模型调用失败时，result() 也会重新抛出对应异常。
                        event = next_chunk.result()
                    except StopAsyncIteration:
                        # 异步迭代器没有更多数据，说明图已经执行结束。
                        break
                    yield event
        finally:
            with anyio.CancelScope(shield=True):
                if next_chunk:
                    if not next_chunk.done():
                        next_chunk.cancel()
                    # cancel 只是发出取消请求，gather 等待它退出；收集异常以免打断后续关闭操作。
                    await asyncio.gather(next_chunk, return_exceptions=True)
                # 显式关闭图迭代器，让模型客户端也结束对上游流的读取。
                await iterator.aclose()

        # 保存期间也保持心跳；断开时取消保存任务，让未提交的事务回滚。
        save_task = asyncio.create_task(self.save_reply(self.reply))
        try:
            # 保存也可能需要等待，沿用同样的心跳机制；任务失败时由 result() 抛出异常。
            while not (await asyncio.wait({save_task}, timeout=self.heartbeat_seconds))[0]:
                yield ": ping\n\n"
            result = save_task.result()
        finally:
            with anyio.CancelScope(shield=True):
                if not save_task.done():
                    save_task.cancel()
                # 等待保存任务退出，确保其数据库会话完成关闭或回滚，再执行外层占用清理。
                await asyncio.gather(save_task, return_exceptions=True)
        # 只有数据库提交成功才发送 done；mode="json" 将日期等字段转成可序列化的数据。
        yield sse_event("done", result.model_dump(mode="json"))

    async def save_reply(self, reply: str):
        """独立短会话保存结果，记录提交成功，断开时不删除已经提交的问答。"""
        # 此时才创建数据库会话，等待模型期间不占用数据库连接。
        async with self.sessions() as db:
            result = await save_generation(db, self.generation, reply)
        # save_generation 已经提交事务；即使之后 done 发送失败，问答仍然是有效的历史记录。
        self.completed = True
        return result

    async def events(self):
        """完成请求重放保存结果，其余请求转发模型事件和安全的错误提示。"""
        request_id = str(self.generation.body.request_id)
        try:
            if self.generation.saved:
                # 同一 request_id 已经保存成功：重放原结果，避免网络重试再次调用模型。
                saved = self.generation.saved
                yield sse_event("delta", {"request_id": request_id, "text": saved.messages[-1].content})
                yield sse_event("done", saved.model_dump(mode="json"))
            else:
                # aclosing 确保外层被取消或关闭时，也会关闭内部 model_events 生成器。
                # async for 逐个读取事件，yield 再把事件交给父类的发送循环。
                async with aclosing(self.model_events()) as events:
                    async for event in events:
                        yield event
        # 流已开始后不能再修改 HTTP 状态，通过 error 事件通知前端。
        # 取消异常会继续向外传播并进入 finally 清理，不会作为模型错误发送。
        except TimeoutError:
            self.failed = True
            logger.warning("流式回答超时 会话=%s 消息请求=%s", self.generation.conversation_id, request_id)
            yield sse_event("error", {"request_id": request_id, "code": "timeout", "message": "回答超时，请重试"})
        except ModelError as exc:
            self.failed = True
            logger.warning("流式回答模型错误 会话=%s 消息请求=%s 原因=%s",
                           self.generation.conversation_id, request_id, exc)
            yield sse_event("error", {"request_id": request_id, "code": "model_error", "message": str(exc)})
        except SQLAlchemyError:
            self.failed = True
            logger.error("流式回答保存失败 会话=%s 消息请求=%s",
                         self.generation.conversation_id, request_id, exc_info=True)
            yield sse_event("error", {"request_id": request_id, "code": "save_error", "message": "回答保存失败，请重试"})
        except HTTPException:
            self.failed = True
            logger.warning("流式回答请求已过期 会话=%s 消息请求=%s", self.generation.conversation_id, request_id)
            yield sse_event("error", {"request_id": request_id, "code": "request_expired", "message": "本次请求已过期，请重新打开会话"})
        except Exception:
            self.failed = True
            logger.exception("流式生成异常 会话=%s 消息请求=%s", self.generation.conversation_id, request_id)
            yield sse_event("error", {"request_id": request_id, "code": "model_error", "message": "生成失败，请重试"})
