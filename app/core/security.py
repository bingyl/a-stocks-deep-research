"""密码哈希与 JWT。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings


def hash_password(password: str) -> str:
    raw = (password or "").encode("utf-8")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            (password or "").encode("utf-8"),
            (password_hash or "").encode("ascii"),
        )
    except (ValueError, TypeError):
        return False


def create_access_token(*, user_id: int, username: str) -> str:
    settings = get_settings()
    settings.require_auth_secret()
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=max(5, int(settings.jwt_expire_minutes or 60)))
    payload = {
        "sub": str(int(user_id)),
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    settings.require_auth_secret()
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        options={"require": ["exp", "sub"]},
    )
