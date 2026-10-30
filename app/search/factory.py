"""根据配置选择搜索服务，新增服务时只扩展适配器和这里的注册。"""

import httpx

from app.core.settings import Settings
from app.llm.errors import ModelConfigurationError
from app.search.base import SearchProvider
from app.search.ddgs import DdgsSearch
from app.search.tavily import TavilySearch


def create_search_provider(settings: Settings, client: httpx.AsyncClient) -> SearchProvider | None:
    """按配置选择免费 DDGS 或 Tavily；Tavily 未填写 Key 时不启用搜索。"""
    provider = settings.search_provider.strip().lower()
    if provider == "ddgs":
        return DdgsSearch()
    if provider == "tavily":
        key = settings.tavily_api_key.get_secret_value().strip()
        return TavilySearch(key, client) if key else None
    raise ModelConfigurationError(f"尚未接入搜索服务 {provider!r}，请检查 SEARCH_PROVIDER")
