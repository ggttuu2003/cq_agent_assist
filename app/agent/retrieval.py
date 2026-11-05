"""会话开启知识库时固定执行的检索节点，不依赖模型选择工具。"""

import logging
import re
from time import perf_counter

from app.services.traces import trace_context

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


def needs_question_context(text: str) -> bool:
    """只识别明确指代或省略主题的中文追问，不用字数猜测语义。

    这是保守规则，不是完整的语义理解；不明确的问题直接检索自身。
    完整的新问题如“如何缓解过拟合”不会匹配省略主题的表达。
    """
    question = text.strip().rstrip("？?！!。.").strip()
    if re.match(r"^(?:那|那么)?(?:它们?|上述|前面|刚才|上面)", question):
        return True
    if re.match(r"^(?:那|那么)?(?:这(?:个|些|种)?|那(?:个|些|种))(?:的|有什么|有何|怎么|如何|是指|是什么意思|呢$)", question):
        return True
    if re.fullmatch(r"(?:那|那么)?第[一二三四五六七八九十\d]+(?:种|个|条|步)(?:方法|方案|方式|步骤)?(?:呢|怎么样|怎么用|怎么实现)?", question):
        return True
    # 必须整句匹配：问句带了具体主题时，不再补充历史话题。
    return bool(re.fullmatch(
        r"(?:那|那么|然后)?(?:该|应该|可以|要)?(?:怎么|如何|怎样)"
        r"(?:缓解|解决|优化|使用|实现|处理|配置|选择|避免|改进|验证|操作)"
        r"(?:一下)?(?:呢|啊|呀|吗|比较好|才好)?", question))


def prepare_retrieval(state) -> dict:
    """默认按当前问题检索；明确追问只补用户问题，模型回答不作为检索词。"""
    messages = state["messages"]
    current = message_text(messages[-1]).strip()
    query = current[:400]
    if not needs_question_context(current) or len(current) >= 400:
        return {"retrieval_query": query}

    context = []
    for message in reversed(messages[:-1]):
        # 工具结果、系统消息、模型回复可能冗长或带有错误，不加入查询上下文。
        if message.type != "human":
            continue
        previous = message_text(message).strip()
        if not previous:
            continue
        context.insert(0, previous[:120])
        # 连续追问最多回溯三条，找到最近的独立问题后不再混入更早的话题。
        if len(context) >= 3 or not needs_question_context(previous):
            break
    if context:
        prefix, suffix = "相关用户问题：", f"\n当前问题：{current}"
        budget = min(260, 400 - len(prefix) - len(suffix))
        if budget > 0:
            query = prefix + "\n".join(context)[:budget] + suffix
    return {"retrieval_query": query}


async def retrieve_knowledge(state) -> dict:
    """开启后必经此节点；空结果与失败分别返回，不伪造引用，也不阻断普通回答。"""
    config = get_config().get("configurable", {})
    service, user_id = config.get("knowledge_service"), config.get("knowledge_user_id")
    query = state["retrieval_query"]
    logger.info("知识检索开始 用户=%s 查询字符数=%s", user_id, len(query))
    trace = trace_context.get()
    if trace is not None:
        await trace.start_retrieval(query)
    started = perf_counter()
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
    if trace is not None:
        await trace.retrieval(query, result, round((perf_counter() - started) * 1000))
    return {"retrieval": result, "citations": result["citations"]}
