"""用户账号服务。"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import log_caught
from app.core.security import hash_password, verify_password
from app.persistence.db import async_session_scope, init_db
from app.persistence.db.models import User

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\u4e00-\u9fff]{2,32}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def validate_username(username: str) -> str:
    name = (username or "").strip()
    if not _USERNAME_RE.match(name):
        raise ValueError("用户名需 2–32 位，仅字母/数字/下划线/中文")
    return name


def validate_password(password: str) -> str:
    pwd = password or ""
    if len(pwd) < 6:
        raise ValueError("密码至少 6 位")
    if len(pwd.encode("utf-8")) > 72:
        raise ValueError("密码过长")
    return pwd


def _user_public(row: User) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "username": row.username,
        "created_at": row.created_at,
    }


async def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(User, int(user_id))
        if not row or int(row.is_active or 0) != 1:
            return None
        return _user_public(row)


async def get_user_by_username(username: str) -> User | None:
    await init_db()
    name = (username or "").strip()
    async with async_session_scope() as session:
        return (
            await session.scalars(select(User).where(User.username == name).limit(1))
        ).first()


async def create_user(username: str, password: str) -> dict[str, Any]:
    await init_db()
    name = validate_username(username)
    pwd = validate_password(password)
    async with async_session_scope() as session:
        exists = (
            await session.scalars(select(User.id).where(User.username == name).limit(1))
        ).first()
        if exists is not None:
            raise ValueError("用户名已存在")
        row = User(
            username=name,
            password_hash=hash_password(pwd),
            created_at=_now_iso(),
            is_active=1,
        )
        session.add(row)
        await session.flush()
        return _user_public(row)


async def set_user_password(username: str, password: str) -> dict[str, Any]:
    """重置已有用户密码。"""
    await init_db()
    name = validate_username(username)
    pwd = validate_password(password)
    async with async_session_scope() as session:
        row = (
            await session.scalars(select(User).where(User.username == name).limit(1))
        ).first()
        if row is None:
            raise ValueError("用户不存在")
        row.password_hash = hash_password(pwd)
        row.is_active = 1
        return _user_public(row)


async def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    await init_db()
    name = (username or "").strip()
    async with async_session_scope() as session:
        row = (
            await session.scalars(select(User).where(User.username == name).limit(1))
        ).first()
        if not row or int(row.is_active or 0) != 1:
            return None
        if not verify_password(password or "", row.password_hash or ""):
            return None
        return _user_public(row)


async def bootstrap_admin_if_needed() -> None:
    """AUTH_ENABLED 且配置了引导账号时：不存在则创建；可选同步密码。"""
    settings = get_settings()
    if not settings.auth_enabled:
        return
    user = (settings.auth_bootstrap_username or "").strip()
    pwd = settings.auth_bootstrap_password or ""
    if not user or not pwd:
        logger.info("auth bootstrap skipped: username/password not configured")
        return
    try:
        existing = await get_user_by_username(user)
        if existing is None:
            created = await create_user(user, pwd)
            logger.info(
                "bootstrap auth user created id=%s username=%s",
                created["id"],
                user,
            )
            return
        if settings.auth_bootstrap_sync_password:
            updated = await set_user_password(user, pwd)
            logger.info(
                "bootstrap auth password synced id=%s username=%s",
                updated["id"],
                user,
            )
        else:
            logger.info(
                "bootstrap auth user already exists id=%s username=%s "
                "(set AUTH_BOOTSTRAP_SYNC_PASSWORD=true to sync password from .env)",
                int(existing.id),
                user,
            )
    except Exception as exc:
        log_caught(logger, "bootstrap auth user failed", exc=exc, level=logging.ERROR)
