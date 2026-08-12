"""异步 Session 上下文与 FastAPI Depends。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.db.factory import get_session_factory


@asynccontextmanager
async def async_session_scope() -> AsyncIterator[AsyncSession]:
    """提交成功事务；异常回滚。"""
    session = get_session_factory()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


# 兼容旧名
session_scope = async_session_scope


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI Depends：请求级 AsyncSession。"""
    async with async_session_scope() as session:
        yield session
