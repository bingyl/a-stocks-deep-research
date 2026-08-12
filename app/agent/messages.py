"""消息文本提取：优先用 langchain BaseMessage.text。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage


def message_text(msg_or_content: Any) -> str:
    """从 Message / MessageChunk / 原始 content 提取纯文本。"""
    if msg_or_content is None:
        return ""
    if isinstance(msg_or_content, BaseMessage):
        return (msg_or_content.text or "").strip()
    # 部分流式 chunk 也有 .text
    text_attr = getattr(msg_or_content, "text", None)
    if isinstance(text_attr, str) and text_attr.strip():
        return text_attr.strip()
    content = getattr(msg_or_content, "content", msg_or_content)
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                parts.append(str(getattr(block, "text") or ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()
