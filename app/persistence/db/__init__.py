"""数据库访问层：异步 ORM（AsyncEngine + AsyncSession）。

DATABASE_URL:
  sqlite:///... 或 sqlite+aiosqlite:///... → sqlite+aiosqlite（业务库）
  postgresql://... → postgresql+psycopg（业务库异步）
"""

from __future__ import annotations

from app.persistence.base import (
    dialect_name,
    ensure_data_dir,
    resolve_database_url,
    uses_async_engine,
)
from app.persistence.db.factory import (
    get_async_engine,
    get_session_factory,
    reset_engine,
    sqlite_file_path,
)
from app.persistence.db.schema import (
    async_upsert_sync_meta,
    init_db,
    init_db_async,
    upsert_sync_meta,
)
from app.persistence.db.session import async_session_scope, get_session, session_scope

__all__ = [
    "async_session_scope",
    "async_upsert_sync_meta",
    "dialect_name",
    "ensure_data_dir",
    "get_async_engine",
    "get_session",
    "get_session_factory",
    "init_db",
    "init_db_async",
    "reset_engine",
    "resolve_database_url",
    "session_scope",
    "sqlite_file_path",
    "upsert_sync_meta",
    "uses_async_engine",
]
