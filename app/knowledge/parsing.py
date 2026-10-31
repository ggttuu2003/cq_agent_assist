"""TXT 和 Markdown 的文字校验与可定位分段，不依赖模型或数据库。"""

import re
from dataclasses import dataclass


class KnowledgeError(Exception):
    """可以直接向用户展示的资料处理错误，不能夹带上游响应或密钥。"""


@dataclass(slots=True)
class TextChunk:
    """保存正文及原文偏移，来源点击后可以定位到同一段内容。"""
    start: int
    end: int
    location: str
    content: str


def decode_text(data: bytes) -> str:
    """优先 UTF-8，再兼容 GB18030；二进制、空正文和过大正文给出明确提示。"""
    if not data or b"\x00" in data:
        raise KnowledgeError("文件没有有效文字，或实际内容不是 TXT / Markdown")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            content = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise KnowledgeError("无法识别文件编码，请另存为 UTF-8 后重新上传")
    if not content.strip():
        raise KnowledgeError("文件没有可提取的文字")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in content):
        raise KnowledgeError("文件包含二进制控制字符，请上传有效文本")
    if len(content) > 2_000_000:
        raise KnowledgeError("文本内容过长，请拆成较小文件后上传")
    return content.replace("\r\n", "\n").replace("\r", "\n")


def split_text(content: str, size: int, overlap: int) -> list[TextChunk]:
    """优先在段落边界切分；相邻片段适量重叠，偏移始终对应原文。"""
    if not 0 <= overlap < size or size < 100:
        raise KnowledgeError("分段配置不正确，需要满足 0 ≤ 重叠长度 < 片段长度")
    headings = list(re.finditer(r"(?m)^#{1,6}\s+(.+)$", content))
    result, start, heading_index = [], 0, -1
    while start < len(content):
        end = min(len(content), start + size)
        if end < len(content):
            boundary = content.rfind("\n\n", start + size // 2, end)
            if boundary >= 0:
                end = boundary + 2
        text = content[start:end]
        while heading_index + 1 < len(headings) and headings[heading_index + 1].start() <= start:
            heading_index += 1
        if text.strip():
            heading = headings[heading_index].group(1).strip() if heading_index >= 0 else ""
            location = f"第 {len(result) + 1} 段" + (f" · {heading}" if heading else "")
            result.append(TextChunk(start, end, location[:500], text))
        if len(result) > 6000:
            raise KnowledgeError("资料分段过多，请拆成较小文件后上传")
        if end == len(content):
            break
        start = max(start + 1, end - overlap)
    return result
