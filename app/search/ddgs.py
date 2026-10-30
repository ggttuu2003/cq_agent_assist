"""使用无需搜索 Key 的 DDGS，并让同步搜索支持异步等待与真正取消。"""

import anyio
from anyio import to_process
from ddgs import DDGS
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from app.search.base import SearchError, SearchResult, normalize_results


def run_ddgs_search(query: str) -> list[SearchResult]:
    """在工作进程执行同步搜索；函数放在模块顶层，供 Windows 等平台导入执行。"""
    try:
        # timeout 限制搜索源的网络等待；整轮时间由外层 DdgsSearch 再限制。
        with DDGS(timeout=10) as search:
            rows = search.text(query, max_results=5)
    except RatelimitException:
        raise SearchError("搜索源暂时限流，请稍后重试") from None
    except TimeoutException:
        raise SearchError("网页搜索超时，请稍后重试") from None
    except DDGSException as exc:
        # DDGS 没有匹配结果时会抛出这个明确提示，按空结果处理，而不是假装网络失败。
        if str(exc) == "No results found.":
            return []
        raise SearchError("DDGS 搜索失败，可能是网络问题或搜索源限制，请稍后重试") from None
    if not isinstance(rows, list):
        raise SearchError("搜索服务返回的数据格式不正确")
    # DDGS 使用 href/body，先映射成统一字段；工具与前端不需要感知这些差异。
    return normalize_results([
        {"title": item.get("title"), "url": item.get("href"), "content": item.get("body")}
        for item in rows if isinstance(item, dict)
    ])


class DdgsSearch:
    """遵循统一搜索接口，不需要 API Key 或单独部署搜索服务。"""

    timeout_seconds = 15

    async def search(self, query: str) -> list[SearchResult]:
        """把同步检索交给可取消工作进程，等待期间不阻塞 FastAPI 的事件循环。"""
        try:
            with anyio.fail_after(self.timeout_seconds):
                # cancellable=True：用户停止或任务超时时，AnyIO 会终止该工作进程。
                # 工作进程正常结束后可复用；生命周期由 AnyIO 管理，兼容 Windows 和 macOS。
                return await to_process.run_sync(run_ddgs_search, query, cancellable=True)
        except TimeoutError:
            raise SearchError("网页搜索超时，请稍后重试") from None
        except anyio.BrokenWorkerProcess:
            raise SearchError("搜索进程异常退出，请稍后重试") from None
