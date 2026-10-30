"""各家搜索服务统一使用的结果和接口。"""

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlsplit


@dataclass(slots=True)
class SearchResult:
    """一条搜索结果，屏蔽各家服务不同的响应字段。"""

    title: str
    url: str
    content: str


class SearchError(Exception):
    """可以展示给模型和用户的搜索失败提示，不包含密钥或上游原始正文。"""


class SearchProvider(Protocol):
    """搜索服务只需实现这个方法，工具与图无需知道服务商是谁。"""

    async def search(self, query: str) -> list[SearchResult]:
        """根据查询词返回统一格式的结果；失败时抛出 SearchError。"""
        ...


def normalize_results(rows: list[dict]) -> list[SearchResult]:
    """各适配器映射字段后共用校验、去重和长度限制，保持来源格式一致。"""
    results = []
    seen = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        title, url, content = item.get("title"), item.get("url"), item.get("content")
        if not all(isinstance(value, str) for value in (title, url, content)):
            continue
        url = url.strip()
        try:
            address = urlsplit(url)
            if address.scheme not in {"http", "https"} or not address.hostname:
                continue
        except ValueError:
            continue
        # 编码空格和链接分隔字符，避免来源地址破坏 Markdown 结构。
        url = quote(url, safe=":/?#[]@!$&'()*+,;=%")
        if url in seen:
            continue
        seen.add(url)
        results.append(SearchResult(title.strip()[:200] or address.hostname, url, content[:1200]))
        if len(results) == 5:
            break
    return results
