"""Deep Agent / create_agent 监控中间件：日志 + 可选推送 SSE。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import wrap_model_call, wrap_tool_call

from app.agent.sse_bridge import push_sse_event
from app.rag.util import preview_text

logger = logging.getLogger(__name__)


def _tool_call_parts(request: Any) -> tuple[str, Any]:
    tc = getattr(request, "tool_call", None) or {}
    if isinstance(tc, dict):
        return str(tc.get("name") or ""), tc.get("args") or {}
    name = str(getattr(tc, "name", None) or getattr(tc, "get", lambda *_: "")("name") or "")
    args = getattr(tc, "args", None) or {}
    return name, args


@wrap_tool_call
async def monitor_tool(
    request: Any,
    handler: Callable[[Any], Any | Awaitable[Any]],
) -> Any:
    tool_name, tool_args = _tool_call_parts(request)
    args_preview = preview_text(tool_args, 300)
    logger.info(
        "【ToolMiddleWare】工具调用 ToolName=%s ToolArgs=%s",
        tool_name,
        args_preview,
    )
    push_sse_event(
        "middleware_tool",
        {
            "phase": "start",
            "tool": tool_name,
            "arguments": tool_args if isinstance(tool_args, dict) else {},
            "message": f"中间件：调用工具 {tool_name}",
        },
    )
    result = await handler(request)
    logger.info(
        "【ToolMiddleWare】工具调用完成 ToolName=%s Result=%s",
        tool_name,
        preview_text(result, 300),
    )
    push_sse_event(
        "middleware_tool",
        {
            "phase": "end",
            "tool": tool_name,
            "message": f"中间件：工具 {tool_name} 完成",
        },
    )
    return result


@wrap_model_call
async def monitor_model(
    request: Any,
    handler: Callable[[Any], Any | Awaitable[Any]],
) -> Any:
    model = getattr(request, "model", None)
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None) or type(model).__name__
    messages = getattr(request, "messages", None) or []
    logger.info(
        "【ModelMiddleWare】模型调用 model=%s messages=%s",
        model_name,
        len(messages) if hasattr(messages, "__len__") else "-",
    )
    push_sse_event(
        "middleware_model",
        {
            "phase": "start",
            "model": str(model_name),
            "message_count": len(messages) if hasattr(messages, "__len__") else 0,
            "message": f"中间件：模型调用 {model_name}",
        },
    )
    response = await handler(request)
    logger.info(
        "【ModelMiddleWare】模型调用完成 model=%s Response=%s",
        model_name,
        preview_text(response, 300),
    )
    push_sse_event(
        "middleware_model",
        {
            "phase": "end",
            "model": str(model_name),
            "message": f"中间件：模型返回 {model_name}",
        },
    )
    return response


MONITOR_MIDDLEWARE = (monitor_tool, monitor_model)
