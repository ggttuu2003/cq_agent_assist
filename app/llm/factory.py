"""根据配置创建 LangChain 模型客户端。"""

from urllib.parse import urlsplit

import httpx
from langchain_openai import ChatOpenAI

from app.core.settings import Settings
from app.llm.errors import ModelConfigurationError


def create_chat_model(settings: Settings, client: httpx.AsyncClient) -> ChatOpenAI:
    """读取服务地址、密钥和模型名，创建供 LangGraph 节点使用的模型客户端。

    ChatOpenAI 支持兼容协议；实际请求发往 base_url 指定的服务，当前是百炼。
    这个函数只创建客户端，真正发送消息发生在调用 ainvoke 时。
    """
    base_url = settings.llm_base_url.strip().rstrip("/")
    # SecretStr 平时隐藏密钥，只有发送请求需要使用时才取出真实值。
    api_key = settings.llm_api_key.get_secret_value().strip()
    model_name = settings.llm_model.strip()

    if not all((base_url, api_key, model_name)):
        raise ModelConfigurationError("请配置 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL")

    address = urlsplit(base_url)
    if address.scheme not in {"http", "https"} or not address.hostname:
        raise ModelConfigurationError("LLM_BASE_URL 必须是完整的 HTTP 或 HTTPS 地址")

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=settings.llm_timeout,
        max_retries=0,  # 失败时直接返回错误，当前不自动重试。
        http_async_client=client,  # 复用应用创建的异步网络客户端。
        # 保留网络客户端默认的代理行为。
        http_socket_options=(),
        # 百炼使用兼容的对话接口，明确关闭 Responses 接口。
        use_responses_api=False,
        stream_usage=False,
    )
