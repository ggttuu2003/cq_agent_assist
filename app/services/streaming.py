"""把会话生成结果转换为 SSE，并管理连接断开后的取消与清理。"""

import asyncio
import json
import logging
from contextlib import aclosing

import anyio
from fastapi import HTTPException
from langchain_core.messages import AIMessageChunk
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
                            logger.exception("流式生成占用清理失败，将由占用到期机制恢复")

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

    async def model_events(self):
        """读取真实模型片段；等待时发送心跳，结束时关闭图迭代器。"""
        generation = self.generation
        # *history 展开已有消息，再追加本次问题；messages 模式让图逐段返回模型消息。
        iterator = self.agent.astream({
            "messages": [*generation.history, ("user", generation.body.message)],
        }, stream_mode="messages")
        next_chunk = None
        # 片段一边发送一边在内存中累积，模型正常结束后再一次性保存完整回答。
        parts = []
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
                        message, metadata = next_chunk.result()
                    except StopAsyncIteration:
                        # 异步迭代器没有更多数据，说明图已经执行结束。
                        break
                    # metadata 标明片段来源；这里只把模型节点的消息片段作为回答展示。
                    if metadata.get("langgraph_node") != "model" or not isinstance(message, AIMessageChunk):
                        continue
                    text = message.text
                    if text:
                        parts.append(text)
                        # yield 把本次新增文字交给外层发送，不等待完整回答，也不重复发送累计全文。
                        yield sse_event("delta", {"request_id": str(generation.body.request_id), "text": text})
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
        save_task = asyncio.create_task(self.save_reply("".join(parts)))
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
            yield sse_event("error", {"request_id": request_id, "code": "timeout", "message": "回答超时，请重试"})
        except ModelError as exc:
            yield sse_event("error", {"request_id": request_id, "code": "model_error", "message": str(exc)})
        except SQLAlchemyError:
            yield sse_event("error", {"request_id": request_id, "code": "save_error", "message": "回答保存失败，请重试"})
        except HTTPException:
            yield sse_event("error", {"request_id": request_id, "code": "request_expired", "message": "本次请求已过期，请重新打开会话"})
        except Exception:
            logger.exception("流式生成异常")
            yield sse_event("error", {"request_id": request_id, "code": "model_error", "message": "生成失败，请重试"})
