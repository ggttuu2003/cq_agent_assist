"""个人资料接口，只负责参数校验、调用业务逻辑和返回结果。"""

from fastapi import APIRouter

from app.dependencies import Db
from app.schemas.profile import ProfileInfo, ProfileUpdate
from app.services.profile import get_or_create_profile, profile_info, update_profile

router = APIRouter(tags=["个人资料"])


@router.get("/profile", response_model=ProfileInfo)
async def get_profile(db: Db):
    """首次访问自动生成默认昵称，后续直接读取已保存资料。"""
    return profile_info(await get_or_create_profile(db))


@router.put("/profile", response_model=ProfileInfo)
async def update_profile_route(body: ProfileUpdate, db: Db):
    """保存页面提交的昵称。"""
    return profile_info(await update_profile(db, body.display_name))
