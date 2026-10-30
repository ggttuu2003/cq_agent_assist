"""统一终端日志，并为一次 HTTP 请求的所有业务日志关联相同编号。"""

import logging
import sys
import traceback
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send


# ContextVar 按异步任务隔离，并发请求不会互相覆盖编号；子任务会继承当前编号。
request_id_context: ContextVar[str] = ContextVar("log_request_id", default="-")
logger = logging.getLogger(__name__)


class RequestIdFilter(logging.Filter):
    """给应用日志自动补充请求编号，启动等非请求日志使用横线。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_context.get()
        return True


class SafeFormatter(logging.Formatter):
    """保留排错所需的异常位置，不输出可能包含密钥、SQL 参数的原始异常正文。"""

    def formatException(self, exc_info) -> str:
        frames = traceback.extract_tb(exc_info[2])
        # 不使用默认的异常字符串：SQLAlchemy 等异常可能附带完整 SQL 和聊天内容。
        lines = ["异常堆栈（省略原始异常正文）："]
        lines.extend(f"  {frame.filename}:{frame.lineno}，方法 {frame.name}" for frame in frames)
        lines.append(f"异常类型：{exc_info[0].__name__}")
        return "\n".join(lines)


def configure_logging(level: str = "INFO") -> None:
    """配置 app 下所有模块的日志；重复调用也只保留一个终端输出器。"""
    app_logger = logging.getLogger("app")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(SafeFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | 请求=%(request_id)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    app_logger.handlers.clear()
    app_logger.addHandler(handler)
    app_logger.setLevel(level)
    # 由应用自己输出，不再传给根日志器，避免与服务器日志配置叠加后重复打印。
    app_logger.propagate = False


class RequestLoggingMiddleware:
    """记录完整 HTTP 生命周期，包括 SSE 正文发送、断开和清理的耗时。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # 每个请求创建独立记录对象，避免共享状态，也无需在方法里面定义闭包。
        await HttpRequestLog(scope, receive, send).run(self.app)


class HttpRequestLog:
    """保存单次请求状态，并在收发 ASGI 消息时观察响应状态和连接断开。"""

    def __init__(self, scope: Scope, receive: Receive, send: Send):
        self.scope = scope
        self.original_receive = receive
        self.original_send = send
        self.request_id = str(uuid4())
        self.status = 500
        self.body_finished = False
        self.disconnected = False

    async def send(self, message: Message) -> None:
        """响应头附带请求编号，最后一个正文片段发送成功后才标记响应完成。"""
        if message["type"] == "http.response.start":
            self.status = message["status"]
            message = dict(message)
            headers = [(key, value) for key, value in message.get("headers", [])
                       if key.lower() != b"x-request-id"]
            message["headers"] = [*headers, (b"x-request-id", self.request_id.encode("ascii"))]
        await self.original_send(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            self.body_finished = True

    async def receive(self) -> Message:
        """原样传递请求消息，只记录客户端是否发来了断开通知。"""
        message = await self.original_receive()
        if message["type"] == "http.disconnect":
            self.disconnected = True
        return message

    async def run(self, app: ASGIApp) -> None:
        """在整个请求期间绑定编号，结束后恢复上层任务的上下文。"""
        token = request_id_context.set(self.request_id)
        started = perf_counter()
        failed = False
        # 只记录路径，不记录查询参数、请求头、问题正文和响应正文。
        logger.info("请求开始 method=%s path=%r", self.scope["method"], self.scope["path"])
        try:
            await app(self.scope, self.receive, self.send)
        except Exception:
            failed = True
            logger.error("请求发生未处理异常", exc_info=True)
            raise
        finally:
            outcome = "异常" if failed else "完成" if self.body_finished else "客户端断开" if self.disconnected else "中断"
            log_level = logging.ERROR if failed or self.status >= 500 else logging.WARNING if self.status >= 400 else logging.INFO
            logger.log(log_level, "请求结束 method=%s path=%r status=%s 结果=%s 耗时=%.0fms",
                       self.scope["method"], self.scope["path"], self.status, outcome, (perf_counter() - started) * 1000)
            # reset 必须放在最后，保证结束日志仍带本次请求编号。
            request_id_context.reset(token)
