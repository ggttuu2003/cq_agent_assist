"""附件上传与读取接口，具体存储逻辑由服务层处理。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse

from app.dependencies import CurrentUser, Db
from app.schemas.attachments import AttachmentInfo
from app.services.attachments import read_image, upload_image

router = APIRouter(prefix="/attachments", tags=["附件"])


@router.post("", response_model=AttachmentInfo, status_code=201)
async def upload_attachment(file: Annotated[UploadFile, File()], user: CurrentUser, db: Db):
    """接收一张图片，返回发送消息所需的附件编号和展示资料。"""
    try:
        return await upload_image(db, user.id, file)
    finally:
        await file.close()


@router.get("/{attachment_id}")
async def read_attachment(attachment_id: UUID, user: CurrentUser, db: Db):
    """校验归属后返回图片，禁止浏览器猜测为其他类型的文件。"""
    attachment, path = await read_image(db, user.id, str(attachment_id))
    return FileResponse(path, media_type=attachment.media_type,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
