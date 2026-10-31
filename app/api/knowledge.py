"""知识资料 HTTP 接口，校验参数后交给业务层和后台任务。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.dependencies import CurrentUser, Db
from app.knowledge.parsing import KnowledgeError
from app.schemas.knowledge import KnowledgeDocumentInfo, KnowledgePage, KnowledgeQuery, KnowledgeRename
from app.services import knowledge as service

router = APIRouter(prefix="/knowledge", tags=["知识库"])



@router.get("")
async def summary(request: Request, user: CurrentUser):
    """展示个人知识资料数量及配置状态。"""
    return await request.app.state.knowledge.summary(user.id)



@router.get("/documents", response_model=KnowledgePage)
async def list_documents(user: CurrentUser, db: Db, query: str = Query(default="", max_length=200),
                         status: str | None = Query(default=None, max_length=30),
                         cursor: str | None = Query(default=None, max_length=500), limit: int = Query(default=20, ge=1, le=50)):
    """分页读取轻量资料列表。"""
    return await service.list_documents(db, user.id, query.strip(), status, cursor, limit)



@router.post("/documents", response_model=KnowledgeDocumentInfo, status_code=202)
async def upload(user: CurrentUser, db: Db, file: Annotated[UploadFile, File()]):
    """上传后自动排入处理队列，不要求创建或选择知识库。"""
    return await service.upload_document(db, user.id, file)



@router.get("/documents/{document_id}", response_model=KnowledgeDocumentInfo)
async def document(document_id: UUID, user: CurrentUser, db: Db):
    """返回资料及最近任务状态。"""
    return await service.document_info(db, await service.owned_document(db, user.id, str(document_id)))



@router.patch("/documents/{document_id}", response_model=KnowledgeDocumentInfo)
async def rename(document_id: UUID, body: KnowledgeRename, user: CurrentUser, db: Db):
    """修改显示名称，不重建向量。"""
    return await service.rename_document(db, user.id, str(document_id), body.name)



@router.post("/documents/{document_id}/versions", response_model=KnowledgeDocumentInfo, status_code=202)
async def replace(document_id: UUID, user: CurrentUser, db: Db, file: Annotated[UploadFile, File()]):
    """显式替换资料内容，新版本成功后生效。"""
    return await service.upload_document(db, user.id, file, str(document_id))



@router.post("/documents/{document_id}/reprocess", response_model=KnowledgeDocumentInfo, status_code=202)
async def reprocess(document_id: UUID, user: CurrentUser, db: Db):
    """使用已保存文件重试失败任务或重建索引。"""
    return await service.reprocess_document(db, user.id, str(document_id))



@router.delete("/documents/{document_id}", response_model=KnowledgeDocumentInfo, status_code=202)
async def remove(document_id: UUID, user: CurrentUser, db: Db):
    """立即退出检索范围，由后台继续清理文件和向量。"""
    return await service.delete_document(db, user.id, str(document_id))



@router.get("/documents/{document_id}/content")
async def content(document_id: UUID, user: CurrentUser, db: Db, version_id: UUID | None = None,
                  offset: int = Query(default=0, ge=0), limit: int = Query(default=12000, ge=1, le=20000)):
    """分页预览正文；历史引用明确区分旧版本、已清理版本和当前版本。"""
    return await service.document_content(db, user.id, str(document_id), str(version_id) if version_id else None, offset, limit)


@router.get("/documents/{document_id}/chunks")
async def chunks(document_id: UUID, user: CurrentUser, db: Db, version_id: UUID | None = None,
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=50), chunk_id: UUID | None = None):
    """按段落分页预览，原文偏移可用于引用定位。"""
    return await service.document_chunks(db, user.id, str(document_id), str(version_id) if version_id else None,
                                         offset, limit, str(chunk_id) if chunk_id else None)


@router.get("/documents/{document_id}/file")
async def original(document_id: UUID, user: CurrentUser, db: Db):
    """下载当前版本原文件，服务端按编号定位，拒绝任意磁盘路径。"""
    path, filename, media_type = await service.original_file(db, user.id, str(document_id))
    return FileResponse(path, filename=filename, media_type=media_type)


@router.get("/jobs/{job_id}")
async def job_status(job_id: UUID, user: CurrentUser, db: Db):
    """任务也通过资料归属校验，不能用猜测的编号读取其他资料处理状态。"""
    return await service.job_info(db, user.id, str(job_id))


@router.post("/retrieve")
async def retrieve(body: KnowledgeQuery, request: Request, user: CurrentUser):
    """直接试搜个人资料，与 Agent 工具共用检索服务。"""
    try:
        return {"citations": await request.app.state.knowledge.search(user.id, body.query)}
    except KnowledgeError as exc:
        raise HTTPException(503, str(exc)) from None
