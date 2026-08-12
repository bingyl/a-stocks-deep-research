"""RAG 辅助：解析 LangChain tool 事件输出、日志预览截断。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.logging import log_caught

logger = logging.getLogger(__name__)


def extract_tool_output(data: dict[str, Any] | None) -> Any:
    """从 on_tool_end 的 data 中取出工具返回内容。"""
    if not data:
        return None
    output = data.get("output")
    if output is None:
        output = data.get("result")
    # ToolMessage / AIMessage chunk
    if hasattr(output, "content"):
        return getattr(output, "content")
    # 少数实现把结果放在 output["output"] / ["content"]
    if isinstance(output, dict):
        if "content" in output and len(output) <= 3:
            return output.get("content")
        if "output" in output and "content" not in output:
            return output.get("output")
    return output


def preview_text(value: Any, n: int = 300) -> str:
    """日志/预览用：压成单行并截断到约 n 字。"""
    if isinstance(value, (dict, list)):
        try:
            s = json.dumps(value, ensure_ascii=False, default=str)
        except Exception as exc:
            log_caught(
                logger,
                "preview_text json.dumps 失败，回退 str",
                exc=exc,
                level=logging.DEBUG,
            )
            s = str(value)
    else:
        s = str(value or "")
    s = s.replace("\n", " ").replace("\r", " ").strip()
    if len(s) <= n:
        return s
    return s[: max(1, n - 1)] + "…"
