"""认证依赖：JWT Bearer；AUTH_ENABLED=false 时跳过。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.services import users as users_svc

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: int
    username: str


async def get_current_user_optional(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> CurrentUser | None:
    settings = get_settings()
    if not settings.auth_enabled:
        return None
    if creds is None or (creds.scheme or "").lower() != "bearer" or not creds.credentials:
        return None
    try:
        payload = decode_access_token(creds.credentials)
        uid = int(payload.get("sub") or 0)
    except (PyJWTError, TypeError, ValueError):
        return None
    if uid <= 0:
        return None
    user = await users_svc.get_user_by_id(uid)
    if not user:
        return None
    return CurrentUser(id=int(user["id"]), username=str(user["username"]))


async def get_current_user(
    user: CurrentUser | None = Depends(get_current_user_optional),
) -> CurrentUser | None:
    """AUTH_ENABLED 时必须登录；关闭时返回 None（兼容旧行为）。"""
    settings = get_settings()
    if settings.auth_enabled and user is None:
        raise HTTPException(
            status_code=401,
            detail="未登录或令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def scoped_user_id(user: CurrentUser | None) -> int | None:
    """开启认证时返回用户 id，用于报告隔离与同 code 互斥；关闭时为 None。"""
    settings = get_settings()
    if not settings.auth_enabled:
        return None
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或令牌无效")
    return int(user.id)
