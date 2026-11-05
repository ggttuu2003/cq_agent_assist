"""调用记录查询接口，只处理参数和响应。"""

from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.dependencies import CurrentUser, Db
from app.schemas.traces import RequestTraceInfo
from app.services.traces import get_request_trace, original_request

router = APIRouter(prefix="/conversations", tags=["调用记录"])


@router.get("/{conversation_id}/requests/{request_id}/trace", response_model=RequestTraceInfo)
async def request_trace(conversation_id: UUID, request_id: UUID, user: CurrentUser, db: Db):
    """返回一次提问的所有执行及模型调用，失败重试也可查看。"""
    return await get_request_trace(db, user.id, str(conversation_id), str(request_id))


@router.get("/{conversation_id}/requests/{request_id}/trace/calls/{call_id}/request")
async def model_request(conversation_id: UUID, request_id: UUID, call_id: UUID, user: CurrentUser, db: Db):
    """返回恢复图片后的完整请求，供页面复制和下载。"""
    body = await original_request(db, user.id, str(conversation_id), str(request_id), str(call_id))
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
