"""个人资料的默认创建和修改逻辑。"""

import secrets
import logging

from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.schemas.profile import ProfileInfo

logger = logging.getLogger(__name__)


def default_display_name() -> str:
    """生成短随机昵称；昵称由后端生成并保存，前端不会另造一份默认值。"""
    return f"CQ用户-{secrets.token_hex(3).upper()}"


async def get_or_create_profile(db: AsyncSession) -> User:
    """第一次访问时自动建立唯一资料，并发访问也不会覆盖先写入的昵称。"""
    # 日常读取只执行查询，避免每次访问都写入、加锁和提交事务。
    user = await db.get(User, 1)
    if user is not None:
        return user
    candidate = default_display_name()
    statement = insert(User).values(id=1, display_name=candidate)
    statement = statement.on_duplicate_key_update(id=User.id)
    await db.execute(statement)
    await db.commit()
    logger.info("本地个人资料初始化完成 用户=1")
    return await db.get(User, 1)


async def update_profile(db: AsyncSession, display_name: str) -> User:
    """保存用户修改后的昵称。"""
    user = await get_or_create_profile(db)
    user.display_name = display_name
    await db.commit()
    logger.info("个人昵称修改成功 用户=%s", user.id)
    return user


def profile_info(user: User) -> ProfileInfo:
    """转换为页面可以看到的字段，避免暴露数据库模型。"""
    return ProfileInfo(id=user.id, display_name=user.display_name)
