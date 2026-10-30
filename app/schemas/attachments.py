"""客户端可见的附件信息，不暴露服务器磁盘路径。"""

from pydantic import BaseModel


class AttachmentInfo(BaseModel):
    """上传和历史消息共用的附件描述。"""

    id: str
    name: str
    media_type: str
    size: int
    url: str
