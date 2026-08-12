"""分析 SSE 侧信道：中间件可推送事件，analyzer 再并入 SSE 流。

隔离模型（多任务并发）：
- 每个分析协程 ``attach_sse_queue()`` 时创建**独立** ``asyncio.Queue``，并写入 ContextVar；
- FastAPI 每个请求是独立 Task，ContextVar 按任务隔离，A 的中间件 ``push`` 只进 A 的队列；
- drain 使用本协程持有的 queue 引用，并丢弃 ``analysis_run_id`` 不匹配的迟到事件（双保险）。
- ``iter_detached``：分析生产协程与 SSE 消费解耦；客户端断开后清空桥接队列并丢弃后续事件，分析继续写库。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextvars import ContextVar, Token
from typing import Any

from app.core.logging import get_log_run_id

logger = logging.getLogger(__name__)

_sse_queue: ContextVar[asyncio.Queue[dict[str, Any]] | None] = ContextVar(
    "analysis_sse_queue",
    default=None,
)

_END = object()


def attach_sse_queue() -> tuple[asyncio.Queue[dict[str, Any]], Token]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    token = _sse_queue.set(queue)
    return queue, token


def reset_sse_queue(token: Token) -> None:
    _sse_queue.reset(token)


def push_sse_event(event: str, data: dict[str, Any] | None = None) -> bool:
    """从中间件等旁路推送 SSE 事件；无队列时静默丢弃。

    自动附带当前 ContextVar 中的 analysis_run_id，供 drain 侧校验。
    """
    queue = _sse_queue.get()
    if queue is None:
        return False
    payload = dict(data or {})
    run_id = get_log_run_id()
    if run_id and not payload.get("analysis_run_id"):
        payload["analysis_run_id"] = run_id
    queue.put_nowait({"event": event, "data": payload})
    return True


def drain_sse_queue(
    queue: asyncio.Queue[dict[str, Any]] | None,
    *,
    analysis_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """取出队列事件；若指定 analysis_run_id，则丢弃不属于本轮的事件。"""
    if queue is None:
        return []
    want = (analysis_run_id or "").strip() or None
    out: list[dict[str, Any]] = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if want:
            got = str((item.get("data") or {}).get("analysis_run_id") or "").strip()
            if got and got != want:
                continue
        out.append(item)
    return out


def _drain_queue(queue: asyncio.Queue[Any]) -> int:
    """丢弃队列中已积压的事件，释放内存。返回丢弃条数。"""
    n = 0
    while True:
        try:
            queue.get_nowait()
            n += 1
        except asyncio.QueueEmpty:
            return n


async def iter_detached(
    source: AsyncIterator[dict[str, Any]],
    *,
    label: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """在独立 Task 中跑分析流；SSE 消费者被取消时，生产端继续跑到结束。

    消费端断开后清空桥接队列，并停止再往队列塞事件（分析仍写库，历史页可查进度）。
    取消分析请走 ``/reports/{id}/cancel``（RunControl.drain），不要依赖断开 SSE。
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    # 单元素 list：让 _produce 闭包可见消费端是否还在
    consumer_alive = [True]

    async def _produce() -> None:
        try:
            async for item in source:
                if not consumer_alive[0]:
                    continue  # 无人消费：丢弃，避免桥接队列膨胀
                await queue.put(item)
        except Exception as exc:
            if consumer_alive[0]:
                await queue.put(exc)
        finally:
            if consumer_alive[0]:
                await queue.put(_END)

    task = asyncio.create_task(
        _produce(),
        name=f"analyze-detached:{label}" if label else "analyze-detached",
    )
    try:
        while True:
            item = await queue.get()
            if item is _END:
                break
            if isinstance(item, Exception):
                raise item
            yield item
    except asyncio.CancelledError:
        consumer_alive[0] = False
        dropped = _drain_queue(queue)
        logger.info(
            "SSE consumer cancelled; analysis continues in background%s"
            " (drained %s queued events)",
            f" ({label})" if label else "",
            dropped,
        )
        raise
    # 故意不 cancel(task)：切换/关闭某一路 SSE 不应杀掉后台分析
