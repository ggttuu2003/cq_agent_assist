"""对话接口的数据结构与校验规则。"""

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    """请求数据结构：FastAPI 将收到的 JSON 转成这个对象，并校验 message 字段。"""

    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Pydantic 校验 message 时自动调用：去掉首尾空格，拒绝空白消息。"""
        value = value.strip()
        if not value:
            raise ValueError("消息不能为空")
        return value


class ChatResponse(BaseModel):
    """响应数据结构：将模型回答序列化为包含 reply 字段的 JSON。"""

    reply: str


class ChatModelInfo(BaseModel):
    """新会话的默认模型信息。"""

    model: str


class ChatModelOption(BaseModel):
    """可选模型及已知的图片输入能力。"""
    model: str
    image_input_supported: bool | None = None


class ChatModelCatalog(BaseModel):
    """页面模型选择器所需的数据，不包含服务地址和密钥。"""
    default_model: str
    models: list[ChatModelOption]
