"""向量模型和 Milvus 的适配器，业务层只使用统一的异步方法。"""

import asyncio
import hashlib
import json
import logging
import math
import re

from langchain_openai import OpenAIEmbeddings
from pymilvus import AsyncMilvusClient, DataType

from app.knowledge.parsing import KnowledgeError

logger = logging.getLogger(__name__)


class TextEmbeddings:
    """复用后端密钥调用独立的 Embedding 模型，不使用聊天模型生成向量。"""

    def __init__(self, settings, client):
        self.settings, self.client = settings, client
        self.model = None
        self.dimensions = settings.embedding_dimensions
        self.error = None
        base_url = (settings.embedding_base_url or settings.llm_base_url).strip().rstrip("/")
        key = settings.embedding_api_key.get_secret_value() or settings.llm_api_key.get_secret_value()
        if not settings.embedding_model.strip() or not base_url or not key.strip():
            self.error = "请配置 EMBEDDING_MODEL；Embedding 地址和密钥可复用现有模型服务"
        if settings.knowledge_chunk_overlap >= settings.knowledge_chunk_size:
            self.error = "分段重叠长度需要小于片段长度"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", settings.milvus_collection):
            self.error = "MILVUS_COLLECTION 需要是以字母或下划线开头的集合前缀"
        # 指纹包含真正影响向量兼容性的设置，不包含密钥。
        self.profile = hashlib.sha256(json.dumps([
            base_url, settings.embedding_model.strip(), self.dimensions,
            settings.knowledge_chunk_size, settings.knowledge_chunk_overlap, "text-v1",
        ]).encode()).hexdigest()
        self.collection = f"{settings.milvus_collection}_kb_{self.profile[:16]}"
        if not self.error:
            # 百炼向量接口每次最多接收 10 条文本；更多输入由客户端自动切批并保持顺序。
            # 这是上游 HTTP 请求的限制，与 worker 每批保存多少片段分别处理。
            self.model = OpenAIEmbeddings(
                model=settings.embedding_model.strip(), dimensions=self.dimensions,
                api_key=key, base_url=base_url, http_async_client=client,
                check_embedding_ctx_length=False, chunk_size=10, max_retries=0, request_timeout=30,
            )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """返回和输入一一对应的向量，严格核对维度及有限数值，异常不泄露上游正文。"""
        if self.error:
            raise KnowledgeError(self.error)
        try:
            vectors = await self.model.aembed_documents(texts)
            if len(vectors) != len(texts) or any(
                len(vector) != self.dimensions or not all(math.isfinite(value) for value in vector)
                for vector in vectors
            ):
                raise KnowledgeError("Embedding 返回的向量数量或维度不正确")
            return vectors
        except KnowledgeError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                message = "Embedding 认证失败，请检查密钥和模型权限"
            elif status == 429:
                message = "Embedding 请求受到限流，请稍后重试"
            elif status == 400:
                message = "Embedding 请求参数被拒绝，请检查模型名称、向量维度和服务限制"
            elif status:
                message = f"Embedding 服务返回 HTTP {status}，请检查模型配置或额度"
            else:
                message = "Embedding 服务连接失败、超时或返回格式错误，请检查网络和配置"
            logger.warning("向量生成失败 异常类型=%s 状态=%s", type(exc).__name__, status)
            raise KnowledgeError(message) from None


class MilvusIndex:
    """按配置指纹隔离物理集合，只保存检索所需编号和向量。"""

    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.lock = asyncio.Lock()

    def connection(self):
        """首次使用才连接，Milvus 不可用不会阻止普通聊天启动。"""
        if self.client is None:
            self.client = AsyncMilvusClient(uri=self.settings.milvus_uri, token=self.settings.milvus_token, timeout=8)
        return self.client

    async def ensure(self, collection: str, dimensions: int):
        """串行建表，并明确声明归属筛选字段，避免依赖动态字段的隐式行为。"""
        try:
            async with self.lock:
                client = self.connection()
                if await client.has_collection(collection):
                    return
                schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
                schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=36)
                schema.add_field("user_id", DataType.INT64)
                schema.add_field("document_id", DataType.VARCHAR, max_length=36)
                schema.add_field("version_id", DataType.VARCHAR, max_length=36)
                schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimensions)
                indexes = client.prepare_index_params()
                indexes.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")
                try:
                    await client.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong")
                except Exception:
                    # 多个进程可能同时创建同一集合，确认集合确实存在后才接受这个竞争结果。
                    if not await client.has_collection(collection):
                        raise
        except Exception:
            logger.warning("Milvus 初始化失败 集合=%s", collection)
            raise KnowledgeError("Milvus 暂时不可用，请检查向量库连接") from None

    async def upsert(self, collection: str, rows: list[dict]):
        """稳定的片段编号允许任务重试覆盖自身数据，不会重复插入。"""
        try:
            await self.connection().upsert(collection, rows, timeout=15)
        except Exception:
            raise KnowledgeError("写入 Milvus 失败，请检查连接并重试") from None

    async def search(self, collection: str, user_id: int, versions: list[str], vector: list[float], limit: int):
        """筛选条件只来自后端核对的用户和版本编号，模型不能传入过滤表达式。"""
        try:
            client = self.connection()
            if not await client.has_collection(collection):
                raise KnowledgeError("知识索引不存在，请重新处理资料")
            rows = await client.search(collection, data=[vector],
                filter=f"user_id == {int(user_id)} and version_id in {json.dumps(versions)}",
                output_fields=["document_id", "version_id"], limit=limit, consistency_level="Strong", timeout=10)
            return rows[0]
        except KnowledgeError:
            raise
        except Exception:
            raise KnowledgeError("Milvus 检索失败，请稍后重试") from None

    async def delete_version(self, collection: str, version_id: str):
        """使用版本中保存的原集合名清理，改过当前配置也能删除旧索引。"""
        if not collection:
            return
        try:
            client = self.connection()
            if await client.has_collection(collection):
                await client.delete(collection, filter=f"version_id == {json.dumps(version_id)}", timeout=10)
        except Exception:
            raise KnowledgeError("清理 Milvus 索引失败，请稍后重试") from None

    async def close(self):
        """应用关闭时释放连接。"""
        if self.client:
            await self.client.close()
