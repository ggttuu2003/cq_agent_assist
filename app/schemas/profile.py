"""本地个人资料的数据结构。"""

from pydantic import BaseModel, Field, field_validator


class ProfileUpdate(BaseModel):
    """修改昵称，不包含账号密码。"""
    display_name: str = Field(min_length=1, max_length=40)

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """去掉首尾空格，拒绝空白昵称。"""
        value = value.strip()
        if not value:
            raise ValueError("昵称不能为空")
        return value


class ProfileInfo(BaseModel):
    """提供给页面展示的本地个人资料。"""
    id: int
    display_name: str
