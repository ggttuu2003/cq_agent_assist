"""把统一搜索服务包装成 LangChain 可调用的网页搜索工具。"""

import json
import asyncio
import logging
from dataclasses import asdict
from typing import Annotated
from time import perf_counter

from langchain_core.tools import tool
from langgraph.config import get_config
from pydantic import StringConstraints

from app.search.base import SearchError, SearchProvider

logger = logging.getLogger(__name__)


# @tool 把下方的异步函数注册成工具，函数名就是模型看到的工具名。
# parse_docstring 从 Args 中提取参数说明；response_format 表示同时返回文本和结构化结果。
@tool(parse_docstring=True, response_format="content_and_artifact")
async def web_search(
    # Annotated 保留字符串类型，并附加去除首尾空格和长度校验规则。
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)],
) -> tuple[str, dict]:
    """搜索互联网中的网页资料，适用于最新资讯或需要外部来源的问题。

    Args:
        query: 需要搜索的关键词或问题。

    Returns:
        供模型阅读的搜索结果文本，以及供页面展示的结构化来源数据。
    """
    # get_config() 读取本次运行的后台配置，搜索服务由 dependencies.py 创建并传入。
    # provider 不是模型参数；模型只填写 query，不接触服务配置或 API Key。
    provider: SearchProvider | None = get_config().get("configurable", {}).get("search_provider")
    provider_name = type(provider).__name__ if provider else "未配置"
    started = perf_counter()
    logger.info("网页搜索开始 服务=%s 查询字符数=%s", provider_name, len(query))
    try:
        if provider is None:
            raise SearchError("当前未配置联网搜索服务")
        results = await provider.search(query)
        data = {"query": query, "sources": [asdict(item) for item in results], "error": None}
        logger.info("网页搜索完成 服务=%s 结果数=%s 耗时=%.0fms", provider_name,
                    len(results), (perf_counter() - started) * 1000)
    except SearchError as exc:
        # 搜索失败交回模型解释；取消异常继续向外传播，以便停止操作结束搜索。
        data = {"query": query, "sources": [], "error": str(exc)}
        logger.warning("网页搜索失败 服务=%s 原因=%s 耗时=%.0fms", provider_name,
                       exc, (perf_counter() - started) * 1000)
    except asyncio.CancelledError:
        logger.info("网页搜索取消 服务=%s 耗时=%.0fms", provider_name, (perf_counter() - started) * 1000)
        raise
    except Exception:
        logger.error("网页搜索发生未预期异常 服务=%s 耗时=%.0fms", provider_name,
                     (perf_counter() - started) * 1000, exc_info=True)
        raise
    # 第一个返回值 content 交给模型；第二个 artifact 给页面状态和来源列表使用。
    return json.dumps(data, ensure_ascii=False), data
