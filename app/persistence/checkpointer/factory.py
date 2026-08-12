"""按 DATABASE_URL 方言选择 LangGraph Checkpointer。

分析 Agent 使用 ``astream_events``（异步），必须用 Async* Saver：

- PostgreSQL → ``AsyncPostgresSaver``（与业务库同库）
- SQLite → ``AsyncSqliteSaver``（旁路 ``*.checkpoints.db``，避免与业务库写锁互抢）
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.config import get_settings
from app.core.logging import log_caught
from app.persistence.base import (
    apply_sqlite_concurrency_pragmas_async,
    postgres_psycopg_conninfo,
    resolve_database_url,
    sqlite_checkpointer_path,
)

logger = logging.getLogger(__name__)

_checkpointer: BaseCheckpointSaver | None = None
_pg_pool: AsyncConnectionPool | None = None
_sqlite_conn: Any = None


def _resolve_backend() -> tuple[str, str]:
    resolved = resolve_database_url()
    if resolved.startswith("postgresql"):
        return "postgresql", resolved
    if resolved.startswith("sqlite"):
        return "sqlite", resolved
    raise ValueError(
        "DATABASE_URL 用于 Checkpointer 时仅支持 sqlite / postgresql，"
        f"got={resolved!r}"
    )


async def setup_checkpointer() -> BaseCheckpointSaver:
    """在应用 lifespan 中初始化（可重复调用，已存在则复用）。"""
    global _checkpointer, _pg_pool, _sqlite_conn
    if _checkpointer is not None:
        return _checkpointer

    backend, source = _resolve_backend()
    if backend == "postgresql":
        settings = get_settings()
        conninfo = postgres_psycopg_conninfo(source)
        _pg_pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=max(2, int(settings.db_pool_size or 5)),
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
        )
        await _pg_pool.open()
        saver: BaseCheckpointSaver = AsyncPostgresSaver(conn=_pg_pool)
        await saver.setup()
        _checkpointer = saver
        logger.info(
            "checkpointer ready backend=AsyncPostgresSaver (shared DATABASE_URL)"
        )
        return saver

    # SQLite：旁路文件，checkpoint 高频写不堵 research_reports 更新
    path = sqlite_checkpointer_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sqlite_conn = await aiosqlite.connect(str(path), timeout=60)
    _sqlite_conn.row_factory = aiosqlite.Row
    await apply_sqlite_concurrency_pragmas_async(_sqlite_conn)
    saver = AsyncSqliteSaver(_sqlite_conn)
    await saver.setup()
    _checkpointer = saver
    logger.info(
        "checkpointer ready backend=AsyncSqliteSaver path=%s "
        "(sidecar; business DB stays on DATABASE_URL)",
        path,
    )
    return saver


def analysis_thread_prefix(report_id: int) -> str:
    """与 analyzer 中 thread_id 约定一致：``analysis:r{report_id}:``。"""
    return f"analysis:r{int(report_id)}:"


async def delete_checkpoints_for_report(report_id: int) -> int:
    """删除该报告相关分析线程的全部 checkpoint（抑制 sidecar 膨胀）。"""
    get_checkpointer()
    prefix = analysis_thread_prefix(report_id)
    like = f"{prefix}%"
    thread_ids: list[str] = []

    if _sqlite_conn is not None:
        async with _sqlite_conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
            (like,),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows or []:
            if hasattr(r, "keys"):
                thread_ids.append(str(r["thread_id"]))
            else:
                thread_ids.append(str(r[0]))
    elif _pg_pool is not None:
        async with _pg_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE %s",
                    (like,),
                )
                rows = await cur.fetchall()
                for r in rows or []:
                    if isinstance(r, dict):
                        thread_ids.append(str(r["thread_id"]))
                    else:
                        thread_ids.append(str(r[0]))
    else:
        logger.warning(
            "delete_checkpoints_for_report: no connection for report=%s",
            report_id,
        )
        return 0

    cp = get_checkpointer()
    for tid in thread_ids:
        await cp.adelete_thread(tid)
    return len(thread_ids)


def get_checkpointer() -> BaseCheckpointSaver:
    """返回已 setup 的单例；未初始化时抛错。"""
    if _checkpointer is None:
        raise RuntimeError(
            "Checkpointer 尚未初始化，请在应用 lifespan 中调用 setup_checkpointer()"
        )
    return _checkpointer


async def reset_checkpointer() -> None:
    """关闭连接并清空单例（测试 / 进程退出）。"""
    global _checkpointer, _pg_pool, _sqlite_conn
    _checkpointer = None
    if _sqlite_conn is not None:
        try:
            await _sqlite_conn.close()
        except Exception as exc:
            log_caught(
                logger,
                "close async sqlite checkpointer failed",
                exc=exc,
                level=logging.DEBUG,
            )
        _sqlite_conn = None
    if _pg_pool is not None:
        try:
            await _pg_pool.close()
        except Exception as exc:
            log_caught(
                logger,
                "close async pg checkpointer pool failed",
                exc=exc,
                level=logging.DEBUG,
            )
        _pg_pool = None
