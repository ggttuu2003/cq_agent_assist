"""图片校验、附件存取和模型内容块转换。"""

import base64
import io
import logging
import warnings
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Attachment
from app.schemas.attachments import AttachmentInfo

logger = logging.getLogger(__name__)
ATTACHMENT_DIR = Path(__file__).resolve().parents[2] / "data" / "attachments"
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_IMAGE_TOTAL_SIZE = 10 * 1024 * 1024
MAX_IMAGES = 4
MAX_IMAGE_PIXELS = 16_000_000
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def attachment_info(attachment: Attachment) -> AttachmentInfo:
    """把数据库记录转成接口资料，地址仍是后端路径。"""
    return AttachmentInfo(id=attachment.id, name=attachment.name, media_type=attachment.media_type,
                          size=attachment.size, url=f"/attachments/{attachment.id}")


def validate_image(data: bytes) -> str:
    """检查图片真实内容，限制解码大小，并拒绝损坏图片和动图。"""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                media_type = IMAGE_TYPES.get(image.format)
                if not media_type:
                    raise HTTPException(415, "仅支持 JPEG、PNG、WebP 图片")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "图片像素过大，请缩小后上传（最多 1600 万像素）")
                if getattr(image, "n_frames", 1) > 1:
                    raise HTTPException(415, "暂不支持动图，请上传静态图片")
                image.verify()
            # verify 检查文件结构，load 再检查像素数据是否确实可以解码。
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return media_type
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise HTTPException(413, "图片像素过大，请缩小后上传") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise HTTPException(415, "文件不是有效图片，或图片已经损坏") from None


def write_image(attachment_id: str, data: bytes) -> None:
    """按服务端生成的编号保存，原始文件名不参与文件定位。"""
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    (ATTACHMENT_DIR / attachment_id).write_bytes(data)


async def upload_image(db: AsyncSession, user_id: int, file: UploadFile) -> AttachmentInfo:
    """最多读取限制大小加一个字节，验证通过后保存文件和数据库记录。"""
    data = await file.read(MAX_IMAGE_SIZE + 1)
    if not data:
        raise HTTPException(422, "图片不能为空")
    if len(data) > MAX_IMAGE_SIZE:
        raise HTTPException(413, "单张图片不能超过 5 MB")
    media_type = await anyio.to_thread.run_sync(validate_image, data)
    attachment_id = str(uuid4())
    name = (file.filename or "图片").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable()).strip()[:200] or "图片"
    attachment = Attachment(id=attachment_id, user_id=user_id, name=name, media_type=media_type, size=len(data))
    try:
        await anyio.to_thread.run_sync(write_image, attachment_id, data)
        db.add(attachment)
        await db.commit()
    except BaseException:
        # 数据库未保存成功时，不留下没有记录的磁盘文件。
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync((ATTACHMENT_DIR / attachment_id).unlink, True)
        raise
    logger.info("图片上传成功 附件=%s 类型=%s 大小=%s", attachment_id, media_type, len(data))
    return attachment_info(attachment)


async def get_attachments(db: AsyncSession, user_id: int, ids: list[str]) -> list[Attachment]:
    """统一校验图片数量、归属、是否存在及总大小，返回顺序与请求一致。"""
    if not ids:
        return []
    if len(ids) > MAX_IMAGES or len(set(ids)) != len(ids):
        raise HTTPException(422, "每轮最多 4 张图片，不能重复添加同一附件")
    rows = list(await db.scalars(select(Attachment).where(
        Attachment.id.in_(ids), Attachment.user_id == user_id,
    )))
    if len(rows) != len(ids):
        raise HTTPException(404, "图片附件不存在或不属于当前用户，请重新上传")
    if sum(item.size for item in rows) > MAX_IMAGE_TOTAL_SIZE:
        raise HTTPException(413, "每轮图片总大小不能超过 10 MB")
    by_id = {item.id: item for item in rows}
    return [by_id[item] for item in ids]


async def read_image(db: AsyncSession, user_id: int, attachment_id: str) -> tuple[AttachmentInfo, Path]:
    """查询归属并定位实际文件，路由不需要知道文件存储目录。"""
    attachment = (await get_attachments(db, user_id, [attachment_id]))[0]
    path = ATTACHMENT_DIR / attachment.id
    if not await anyio.to_thread.run_sync(path.is_file):
        raise HTTPException(404, "图片文件已不存在，请重新上传")
    return attachment_info(attachment), path


def message_content(text: str, attachments: list[AttachmentInfo]) -> str | list[dict]:
    """纯文字保留字符串；图片在工作线程读取后变成模型能直接接收的内容块。"""
    if not attachments:
        return text
    blocks = [{"type": "text", "text": text}]
    for item in attachments:
        try:
            encoded = base64.b64encode((ATTACHMENT_DIR / item.id).read_bytes()).decode("ascii")
        except OSError:
            raise HTTPException(409, "图片文件已不可用，请重新上传后发送") from None
        blocks.append({"type": "image_url", "image_url": {"url": f"data:{item.media_type};base64,{encoded}"}})
    return blocks
