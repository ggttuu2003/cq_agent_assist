"""应用配置，只读取当前阶段真正需要的环境变量。"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """读取环境变量和项目根目录的 .env，转换为带类型的配置对象。

    系统环境变量优先于 .env；未提供时使用下方字段的默认值。
    """

    app_env: str = "dev"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # 模型地址、密钥和名称由 .env 提供，切换兼容服务时修改这些配置。
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_timeout: float = 60

    # DDGS 不需要搜索 Key；选择 Tavily 时才读取 tavily_api_key。
    search_provider: str = "ddgs"
    tavily_api_key: SecretStr = SecretStr("")

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_db: str = "cq_agent_assist"

    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: str = ""
    milvus_collection: str = "cq_agent_assist"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


# 缓存首次读取的配置，后续调用直接复用；修改 .env 后需要重启应用。
@lru_cache
def get_settings() -> Settings:
    """缓存配置对象，避免每次请求重复读取文件。"""

    return Settings()
