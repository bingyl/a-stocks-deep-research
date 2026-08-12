"""按 DATABASE_URL 方言选择 LangGraph Store。

- PostgreSQL → 官方 ``PostgresStore``（与业务库同库）
- SQLite → ``SqliteDocStore``（旁路 ``*.docstore.db``，与业务库分离写锁）
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from langgraph.store.base import BaseStore
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import get_settings
from app.core.logging import log_caught
from app.persistence.base import (
    postgres_psycopg_conninfo,
    resolve_database_url,
    sqlite_docstore_path,
)
from app.persistence.docstore.sqlite_store import SqliteDocStore

logger = logging.getLogger(__name__)

_pg_pool: Any = None


def _resolve_backend() -> tuple[str, str]:
    """始终跟随 ``DATABASE_URL``，返回 ``(postgresql|sqlite, 已解析 URL)``。"""
    resolved = resolve_database_url()
    if resolved.startswith("postgresql"):
        return "postgresql", resolved
    if resolved.startswith("sqlite"):
        return "sqlite", resolved
    raise ValueError(
        "DATABASE_URL 用于 DocStore 时仅支持 sqlite / postgresql，"
        f"got={resolved!r}"
    )


def create_doc_store() -> BaseStore:
    """根据 DATABASE_URL 创建 Store（不缓存）。"""
    global _pg_pool
    backend, source = _resolve_backend()
    if backend == "postgresql":
        settings = get_settings()
        conninfo = postgres_psycopg_conninfo(source)
        _pg_pool = ConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=max(2, int(settings.db_pool_size or 5)),
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        store = PostgresStore(conn=_pg_pool)
        store.setup()
        logger.info("docstore ready backend=PostgresStore (shared DATABASE_URL)")
        return store

    path = sqlite_docstore_path(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteDocStore(path)
    logger.info(
        "docstore ready backend=SqliteDocStore path=%s "
        "(sidecar; business DB stays on DATABASE_URL)",
        path,
    )
    return store


@lru_cache
def get_doc_store() -> BaseStore:
    return create_doc_store()


def reset_doc_store() -> None:
    global _pg_pool
    try:
        store = get_doc_store()
        close = getattr(store, "close", None)
        if callable(close):
            close()
    except Exception as exc:
        log_caught(logger, "close docstore failed", exc=exc, level=logging.DEBUG)
    if _pg_pool is not None:
        try:
            _pg_pool.close()
        except Exception as exc:
            log_caught(logger, "close pg pool failed", exc=exc, level=logging.DEBUG)
        _pg_pool = None
    get_doc_store.cache_clear()
