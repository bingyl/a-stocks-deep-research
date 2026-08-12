"""建表：ORM metadata.create_all + 旧库列补齐（全程 AsyncEngine）。"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.core.logging import log_caught
from app.persistence.base import dialect_name
from app.persistence.db.factory import get_async_engine, register_engine_reset_hook
from app.persistence.db.models import Base

logger = logging.getLogger(__name__)

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def _table_columns(bind: Connection, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _apply_report_column_alters(conn: Connection, cols: set[str]) -> None:
    if "status" not in cols:
        conn.execute(
            text(
                "ALTER TABLE research_reports "
                "ADD COLUMN status TEXT NOT NULL DEFAULT 'done'"
            )
        )
    if "status_detail" not in cols:
        conn.execute(
            text(
                "ALTER TABLE research_reports "
                "ADD COLUMN status_detail TEXT NOT NULL DEFAULT ''"
            )
        )
    if "analysis_run_id" not in cols:
        conn.execute(
            text(
                "ALTER TABLE research_reports "
                "ADD COLUMN analysis_run_id TEXT NOT NULL DEFAULT ''"
            )
        )
    if "user_id" not in cols:
        conn.execute(text("ALTER TABLE research_reports ADD COLUMN user_id INTEGER"))
    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_reports_status "
                "ON research_reports(status)"
            )
        )
    except Exception as exc:
        log_caught(
            logger,
            "ensure idx_reports_status skipped",
            exc=exc,
            level=logging.DEBUG,
        )
    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_reports_analysis_run "
                "ON research_reports(analysis_run_id)"
            )
        )
    except Exception as exc:
        log_caught(
            logger,
            "ensure idx_reports_analysis_run skipped",
            exc=exc,
            level=logging.DEBUG,
        )
    try:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_reports_user_id "
                "ON research_reports(user_id)"
            )
        )
    except Exception as exc:
        log_caught(
            logger,
            "ensure idx_reports_user_id skipped",
            exc=exc,
            level=logging.DEBUG,
        )


def _ensure_schema_sync(conn: Connection) -> None:
    Base.metadata.create_all(bind=conn)
    cols = _table_columns(conn, "research_reports")
    if cols:
        _apply_report_column_alters(conn, cols)


async def init_db(*, force: bool = False) -> None:
    """异步建表（业务库唯一入口）。"""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return

    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return

    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(_ensure_schema_sync)

    with _SCHEMA_LOCK:
        _SCHEMA_READY = True
        logger.debug("database schema ready dialect=%s (async)", dialect_name())


# 兼容旧名
init_db_async = init_db


def reset_schema_state() -> None:
    """配合 reset_engine：下次 init_db 会重新 ensure。"""
    global _SCHEMA_READY
    with _SCHEMA_LOCK:
        _SCHEMA_READY = False


register_engine_reset_hook(reset_schema_state)


def upsert_sync_meta(session, key: str, value: str, updated_at: str) -> None:
    """方言安全的 sync_meta upsert（ORM merge；sync/async Session 均可）。"""
    from app.persistence.db.models import SyncMeta

    row = session.get(SyncMeta, key)
    # AsyncSession.get 返回 coroutine —— 调用方若是 async 应 await；
    # 本函数仅在已拿到同步 Connection/或在 run_sync 内使用。
    # 业务侧请改用 async_upsert_sync_meta。
    if hasattr(row, "__await__"):
        raise RuntimeError("请使用 async_upsert_sync_meta（AsyncSession）")
    if row is None:
        session.add(SyncMeta(key=key, value=value, updated_at=updated_at))
    else:
        row.value = value
        row.updated_at = updated_at


async def async_upsert_sync_meta(session, key: str, value: str, updated_at: str) -> None:
    from app.persistence.db.models import SyncMeta

    row = await session.get(SyncMeta, key)
    if row is None:
        session.add(SyncMeta(key=key, value=value, updated_at=updated_at))
    else:
        row.value = value
        row.updated_at = updated_at
