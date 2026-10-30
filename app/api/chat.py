"""单轮对话接口。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.graph.state import CompiledStateGraph

from app.dependencies import get_agent
from app.llm.errors import ModelError
from app.schemas.chat import ChatModelInfo, ChatRequest, ChatResponse


# 收集本模块的接口，最后由 main.py 注册到 FastAPI 应用。
router = APIRouter()


@router.get("/models/default", response_model=ChatModelInfo)
async def get_chat_model_info(request: Request) -> ChatModelInfo:
    """返回新会话的默认模型，已有会话从自己的详情读取模型名。"""
    return ChatModelInfo(model=request.app.state.settings.llm_model.strip())


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    # 参数类型是已编译的图；Depends 让 FastAPI 调用 get_agent 并传入它的返回值。
    agent: Annotated[CompiledStateGraph, Depends(get_agent)],
) -> ChatResponse:
    """处理 POST /chat：接收用户消息，执行对话图，将回答作为 JSON 返回。"""
    try:
        # 将用户消息作为图的初始状态传入；本次请求从这一条消息开始。
        result = await agent.ainvoke({"messages": [("user", body.message)]})
        # 图执行完后，最后一条消息就是模型回答；text 取出其中的文本。
        return ChatResponse(reply=result["messages"][-1].text)
    except ModelError as exc:
        # 模型服务调用失败，转换成 HTTP 502 响应交给调用方。
        raise HTTPException(status_code=502, detail=str(exc)) from None
