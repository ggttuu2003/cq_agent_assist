"""将 Tavily 搜索接口转换成项目统一的搜索结果。"""

import httpx

from app.search.base import SearchError, SearchResult, normalize_results


class TavilySearch:
    """只处理 Tavily 的认证、网络调用和字段转换，不依赖 LangGraph。"""

    def __init__(self, api_key: str, client: httpx.AsyncClient):
        """保存后端密钥并复用应用的网络客户端，不在每次搜索时新建连接池。"""
        self.api_key = api_key
        self.client = client

    async def search(self, query: str) -> list[SearchResult]:
        """读取少量网页摘要，网络或额度错误转成明确的中文提示。"""
        try:
            response = await self.client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query, "search_depth": "basic", "max_results": 5,
                      "include_answer": False, "include_raw_content": False},
                timeout=15,
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            raise SearchError("网页搜索超时，请稍后重试") from None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                raise SearchError("搜索服务认证失败，请检查搜索 API Key") from None
            if status in {402, 432, 433}:
                raise SearchError("搜索服务额度不足，请检查账户额度") from None
            if status == 429:
                raise SearchError("搜索请求过于频繁，请稍后重试") from None
            raise SearchError(f"搜索服务暂时不可用（HTTP {status}）") from None
        except httpx.RequestError:
            raise SearchError("无法连接搜索服务，请检查网络") from None

        try:
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError
        except ValueError:
            raise SearchError("搜索服务返回的数据格式不正确") from None

        return normalize_results(data["results"])
