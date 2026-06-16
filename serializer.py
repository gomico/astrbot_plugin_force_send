import re
from typing import Any

MAX_MESSAGES = 8
MAX_PER_MESSAGE = 1200
MAX_TOTAL = 6000


def serialize_messages(messages: list) -> str:
    """把 run_context.messages 压缩成文本，用于重试提示。

    规则：
    - 默认保留最近 MAX_MESSAGES 条消息
    - 每条消息最多 MAX_PER_MESSAGE 字符
    - 总长度最多 MAX_TOTAL 字符
    - 优先保留 assistant 文本和 tool result 摘要
    - 忽略不可稳定序列化的内容，替换为简短占位
    """
    if not messages:
        return ""

    lines: list[str] = []
    for msg in messages[-MAX_MESSAGES:]:
        content = _get_message_text(msg)
        if not content:
            continue
        if len(content) > MAX_PER_MESSAGE:
            content = content[:MAX_PER_MESSAGE] + "\n...(截断)"
        role = getattr(msg, "role", "unknown")
        lines.append(f"[{role}]: {content}")

    text = "\n\n".join(lines)
    if len(text) > MAX_TOTAL:
        text = text[:MAX_TOTAL] + "\n\n...(截断)"

    return text


def _get_message_text(msg: Any) -> str:
    """尝试从各种消息对象中提取可读文本内容。

    处理不同格式的消息：
    - 有 .content 属性的对象
    - 普通的 dict（含 role/content/text 键）
    - 纯字符串
    - 不可序列化的内容返回空字符串
    """
    if msg is None:
        return ""

    content = None

    # 尝试 dict 风格访问
    if isinstance(msg, dict):
        content = msg.get("content") or msg.get("text") or ""
    elif hasattr(msg, "content"):
        content = msg.content
    else:
        content = str(msg)

    if not content:
        return ""

    content = str(content)

    # 压缩连续空白
    content = re.sub(r"\n{3,}", "\n\n", content)

    # 替换不可序列化内容的占位标记
    content = content.replace("<image>", "[图片]")
    content = content.replace("<audio>", "[音频]")
    content = content.replace("<video>", "[视频]")
    content = content.replace("<file>", "[文件]")

    return content.strip()
