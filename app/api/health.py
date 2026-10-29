"""应用健康检查接口。"""

from fastapi import APIRouter, Request


router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    """返回应用自身状态，不在这一步提前连接数据库。"""
    settings = request.app.state.settings
    return {
        "status": "ok",
        "environment": settings.app_env,
        "mysql": {
            "host": settings.mysql_host,
            "port": settings.mysql_port,
            "database": settings.mysql_db,
        },
        "milvus": {
            "uri": settings.milvus_uri,
            "collection": settings.milvus_collection,
        },
    }
