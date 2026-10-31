"""会话开启知识库时固定执行的检索节点，不依赖模型选择工具。"""

import logging

from langgraph.config import get_config

from app.knowledge.parsing import KnowledgeError

logger = logging.getLogger(__name__)


def entry_node(state) -> str:
    """开关来自本轮已锁定的会话配置，关闭时直接进入模型节点。"""
    enabled = get_config().get("configurable", {}).get("knowledge_enabled", False)
    logger.info("本轮知识库路径 开启=%s", enabled)
    return "prepare_retrieval" if enabled else "model"


def message_text(message) -> str:
    """只提取文字，不把图片地址或 Base64 混入向量查询。"""
    if isinstance(message.content, str):
        return message.content
    return "\n".join(block.get("text", "") for block in message.content
                     if isinstance(block, dict) and block.get("type") == "text")


def prepare_retrieval(state) -> dict:
    """生成有上限的查询词；先完成此节点，客户端就能收到检索开始事件。"""
    messages = state["messages"]
    current = message_text(messages[-1]).strip()
    query = current[:400]
    # 短追问补充最近一轮问答，当前问题放在最后；完整长问题直接检索自身。
    if len(current) < 60 and len(messages) > 1:
        context = "\n".join(message_text(item)[:130] for item in messages[-3:-1])
        query = f"最近对话：{context}\n当前问题：{current}"[:400]
    return {"retrieval_query": query}


async def retrieve_knowledge(state) -> dict:
    """开启后必经此节点；空结果与失败分别返回，不伪造引用，也不阻断普通回答。"""
    config = get_config().get("configurable", {})
    service, user_id = config.get("knowledge_service"), config.get("knowledge_user_id")
    query = state["retrieval_query"]
    logger.info("知识检索开始 用户=%s 查询字符数=%s", user_id, len(query))
    try:
        if service is None or user_id is None:
            raise KnowledgeError("知识库服务尚未准备好")
        citations = await service.search(user_id, query)
        result = {"citations": citations, "error": None,
                  "message": "已检索个人资料" if citations else "未找到相关资料，本轮没有知识库依据"}
    except KnowledgeError as exc:
        result = {"citations": [], "error": str(exc), "message": "知识库检索失败，本轮未获得资料"}
        logger.warning("知识检索失败 用户=%s 原因=%s", user_id, exc)
    logger.info("固定知识检索结束 命中数=%s 失败=%s", len(result["citations"]), bool(result["error"]))
    return {"retrieval": result, "citations": result["citations"]}
