"""把个人知识检索注册为模型可自动调用的标准工具。"""

import json
import logging
from typing import Annotated

from langchain_core.tools import tool
from langgraph.config import get_config
from pydantic import StringConstraints

from app.knowledge.parsing import KnowledgeError

logger = logging.getLogger(__name__)


class CitationContext:
    """一轮执行独有的来源编号表，多次检索同一片段时编号保持一致。"""

    def __init__(self):
        self.items = {}

    def register(self, items: list[dict]) -> list[dict]:
        """工具输出和最终页面使用同一份服务器生成的引用编号。"""
        results = []
        for item in items:
            if item["chunk_id"] not in self.items:
                self.items[item["chunk_id"]] = {**item, "number": len(self.items) + 1}
            results.append(self.items[item["chunk_id"]])
        return results


@tool(parse_docstring=True, response_format="content_and_artifact")
async def knowledge_search(
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)],
) -> tuple[str, dict]:
    """检索用户上传的笔记、项目文档和个人收藏，需要这些资料的具体内容时先调用。

    Args:
        query: 用完整语义描述要找的个人资料或问题，追问时补全所指对象。

    Returns:
        带引用编号的参考资料文本，以及实际命中的结构化来源；空结果和失败会明确说明。
    """
    config = get_config().get("configurable", {})
    service, user_id = config.get("knowledge_service"), config.get("knowledge_user_id")
    context = config.get("knowledge_context")
    logger.info("知识检索开始 用户=%s 查询字符数=%s", user_id, len(query))
    try:
        if service is None or user_id is None or context is None:
            raise KnowledgeError("当前知识检索服务尚未准备好")
        results = context.register(await service.search(user_id, query))
        data = {"query": query, "citations": results, "error": None,
                "message": "已找到参考资料，请用对应编号引用" if results else "个人资料中没有找到相关内容，不能声称资料中有记载"}
    except KnowledgeError as exc:
        logger.warning("知识检索失败 用户=%s 原因=%s", user_id, exc)
        data = {"query": query, "citations": [], "error": str(exc)}
    return json.dumps(data, ensure_ascii=False), data
