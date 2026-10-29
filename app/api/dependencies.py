"""为接口提供所需的对象。"""

from fastapi import HTTPException, Request
from langgraph.graph.state import CompiledStateGraph

from app.agent.graph import build_agent
from app.llm.errors import ModelConfigurationError
from app.llm.factory import create_chat_model


async def get_agent(request: Request) -> CompiledStateGraph:
    """给对话接口提供可执行的图，由 FastAPI 的 Depends 自动调用。

    request.app.state 保存应用共享对象；第一次创建图，以后复用。
    复用的是流程定义，当前没有保存跨请求的聊天记录。
    """
    if request.app.state.agent is not None:
        return request.app.state.agent

    try:
        model = create_chat_model(
            request.app.state.settings,
            request.app.state.http_client,
        )
    except ModelConfigurationError as exc:
        # 地址、密钥或模型名未正确配置，返回 HTTP 503 提示服务尚不可用。
        raise HTTPException(status_code=503, detail=str(exc)) from None

    request.app.state.agent = build_agent(model)
    return request.app.state.agent
