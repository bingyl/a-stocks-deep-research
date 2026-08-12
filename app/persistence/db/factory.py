from __future__ import annotations

import logging
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import log_caught
from app.persistence.base import (
    apply_sqlite_concurrency_pragmas,
    dialect_name,
    ensure_data_dir,
    resolve_database_url,
)

logger = logging.getLogger(__name__)

_ENGINE_RESET_HOOKS: list[Callable[[], None]] = []


def register_engine_reset_hook(fn: Callable[[], None]) -> None:
    """schema 等模块注册：引擎 dispose 后需一并清掉的进程内状态。"""
    if fn not in _ENGINE_RESET_HOOKS:
        _ENGINE_RESET_HOOKS.append(fn)


def _attach_sqlite_pragmas(engine: AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_on_connect(dbapi_conn, _connection_record) -> None:  # type: ignore[no-untyped-def]
        apply_sqlite_concurrency_pragmas(dbapi_conn)


@lru_cache
def get_async_engine() -> AsyncEngine:
    """业务库唯一引擎：AsyncEngine（sqlite+aiosqlite / postgresql+psycopg）。"""
    settings = get_settings()
    url = resolve_database_url(settings.database_url)
    dial = dialect_name(url)

    if dial == "sqlite":
        ensure_data_dir()
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            connect_args={"timeout": 60},
        )
        _attach_sqlite_pragmas(engine)
        return engine

    if dial != "postgresql":
        raise ValueError(f"不支持的数据库方言「{dial}」，仅支持 sqlite / postgresql")

    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_async_engine(),
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )


async def reset_engine() -> None:
    """测试或热更新配置时丢弃缓存引擎。"""
    if get_async_engine.cache_info().currsize:
        try:
            eng = get_async_engine()
            await eng.dispose()
        except Exception as exc:
            log_caught(
                logger,
                "dispose async engine during reset failed",
                exc=exc,
                level=logging.DEBUG,
            )
    get_async_engine.cache_clear()
    get_session_factory.cache_clear()
    for hook in _ENGINE_RESET_HOOKS:
        try:
            hook()
        except Exception as exc:
            log_caught(logger, "engine reset hook failed", exc=exc, level=logging.ERROR)


def sqlite_file_path(url: str | None = None) -> Path | None:
    from app.persistence.base import sqlite_path_from_database_url

    resolved = resolve_database_url(url)
    if not resolved.startswith("sqlite"):
        return None
    return sqlite_path_from_database_url(resolved)
