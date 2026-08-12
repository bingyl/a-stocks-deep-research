"""追问专用工具包装：知识库短路 + 禁止嵌套 tool 事件 + 同参 singleflight。"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from app.core.config import get_settings
from app.core.logging import log_caught
from app.rag.context import get_rag_context
from app.rag.kb_lookup import (
    FOLLOWUP_DEDUP_TOOLS,
    fresh_tool_parents,
    skipped_tool_payload,
    tool_arg_key,
)

logger = logging.getLogger(__name__)

# report_id:tool:arg_key -> 进行中的 Future[str]
_inflight: dict[str, asyncio.Future[str]] = {}
_inflight_lock = asyncio.Lock()


async def _call_tool_raw(tool: BaseTool, kwargs: dict[str, Any]) -> Any:
    """直接调底层函数，避免再 ainvoke 冒泡出第二次 on_tool_*（会双记日志/双入库）。"""
    coro = getattr(tool, "coroutine", None)
    if callable(coro):
        return await coro(**kwargs)
    func = getattr(tool, "func", None)
    if callable(func):
        return await asyncio.to_thread(func, **kwargs)
    # 兜底：关闭 callbacks，尽量不产生子 span
    return await tool.ainvoke(kwargs, config={"callbacks": []})


def _dump_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def _wrap_dedup_tool(tool: BaseTool) -> BaseTool:
    """知识库未过期则短路；同参并发只执行一次。"""
    original = tool
    tool_name = original.name

    async def _arun(**kwargs: Any) -> str:
        settings = get_settings()
        ctx = get_rag_context()
        report_id = str(ctx.report_id) if ctx and ctx.report_id else ""
        flight_key = f"{report_id}:{tool_name}:{tool_arg_key(kwargs)}"

        if (
            settings.rag_followup_enabled()
            and settings.rag_ingest_enabled()
            and report_id
            and tool_name in FOLLOWUP_DEDUP_TOOLS
        ):
            parents = fresh_tool_parents(report_id, tool_name, kwargs)
            if parents:
                payload = skipped_tool_payload(
                    tool=tool_name, arguments=kwargs, parents=parents
                )
                logger.info(
                    "追问短路「%s」：知识库已有 %s 条未过期父片段，跳过重复拉取",
                    tool_name,
                    len(parents),
                )
                return json.dumps(payload, ensure_ascii=False)

        async with _inflight_lock:
            existing = _inflight.get(flight_key)
            if existing is not None:
                waiter: asyncio.Future[str] = existing
                created = False
            else:
                loop = asyncio.get_running_loop()
                waiter = loop.create_future()
                _inflight[flight_key] = waiter
                created = True

        if not created:
            logger.info(
                "追问合并并发「%s」：相同参数已在执行，复用结果（避免双倍成本）",
                tool_name,
            )
            return await waiter

        try:
            raw = await _call_tool_raw(original, kwargs)
            text = _dump_result(raw)
            if not waiter.done():
                waiter.set_result(text)
            return text
        except Exception as exc:
            log_caught(
                logger,
                "followup tool failed name=%s",
                tool_name,
                exc=exc,
                level=logging.ERROR,
            )
            if not waiter.done():
                waiter.set_exception(exc)
            raise
        finally:
            async with _inflight_lock:
                if _inflight.get(flight_key) is waiter:
                    _inflight.pop(flight_key, None)

    return StructuredTool.from_function(
        name=tool_name,
        description=original.description,
        coroutine=_arun,
        args_schema=getattr(original, "args_schema", None),
    )


def wrap_followup_tools(tools: list[Any]) -> list[Any]:
    """对财务/板块等结构化工具套上知识库去重与 singleflight。"""
    out: list[Any] = []
    for t in tools:
        name = getattr(t, "name", "") or ""
        if name in FOLLOWUP_DEDUP_TOOLS:
            out.append(_wrap_dedup_tool(t))
        else:
            out.append(t)
    return out
