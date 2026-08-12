"""认证 API。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.security import create_access_token
from app.deps.auth import CurrentUser, get_current_user
from app.services import users as users_svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthConfigResponse(BaseModel):
    enabled: bool
    allow_register: bool
    username_rule: str = "2–32 位，字母/数字/下划线/中文"
    password_rule: str = "至少 6 位，最长 72 字节"


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    created_at: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    if request.client:
        return request.client.host or "-"
    return "-"


@router.get("/config", response_model=AuthConfigResponse, summary="认证开关（无需登录）")
async def auth_config():
    s = get_settings()
    return AuthConfigResponse(
        enabled=bool(s.auth_enabled),
        allow_register=bool(s.auth_enabled and s.auth_allow_register),
    )


@router.post("/login", response_model=TokenResponse, summary="登录获取 JWT")
async def login(req: LoginRequest, request: Request):
    s = get_settings()
    ip = _client_ip(request)
    username = (req.username or "").strip()
    if not s.auth_enabled:
        logger.warning("auth.login rejected auth_disabled username=%s ip=%s", username, ip)
        raise HTTPException(status_code=400, detail="未开启用户认证")
    try:
        s.require_auth_secret()
    except RuntimeError as exc:
        logger.error("auth.login misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    user = await users_svc.authenticate_user(req.username, req.password)
    if not user:
        logger.warning("auth.login failed username=%s ip=%s", username, ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(user_id=int(user["id"]), username=str(user["username"]))
    logger.info(
        "auth.login ok user_id=%s username=%s ip=%s",
        user["id"],
        user["username"],
        ip,
    )
    return TokenResponse(
        access_token=token,
        user=UserOut(
            id=int(user["id"]),
            username=str(user["username"]),
            created_at=str(user.get("created_at") or ""),
        ),
    )


@router.post("/register", response_model=TokenResponse, summary="注册并登录")
async def register(req: RegisterRequest, request: Request):
    s = get_settings()
    ip = _client_ip(request)
    username = (req.username or "").strip()
    if not s.auth_enabled:
        logger.warning("auth.register rejected auth_disabled username=%s ip=%s", username, ip)
        raise HTTPException(status_code=400, detail="未开启用户认证")
    if not s.auth_allow_register:
        logger.warning("auth.register rejected closed username=%s ip=%s", username, ip)
        raise HTTPException(status_code=403, detail="未开放注册，请联系管理员")
    try:
        s.require_auth_secret()
    except RuntimeError as exc:
        logger.error("auth.register misconfigured: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    try:
        user = await users_svc.create_user(req.username, req.password)
    except ValueError as exc:
        logger.warning(
            "auth.register failed username=%s ip=%s reason=%s",
            username,
            ip,
            exc,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = create_access_token(user_id=int(user["id"]), username=str(user["username"]))
    logger.info(
        "auth.register ok user_id=%s username=%s ip=%s",
        user["id"],
        user["username"],
        ip,
    )
    return TokenResponse(
        access_token=token,
        user=UserOut(
            id=int(user["id"]),
            username=str(user["username"]),
            created_at=str(user.get("created_at") or ""),
        ),
    )


@router.get("/me", response_model=UserOut, summary="当前用户")
async def me(user: CurrentUser | None = Depends(get_current_user)):
    s = get_settings()
    if not s.auth_enabled:
        raise HTTPException(status_code=400, detail="未开启用户认证")
    assert user is not None
    info = await users_svc.get_user_by_id(user.id)
    if not info:
        logger.warning("auth.me failed user_id=%s missing_or_disabled", user.id)
        raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    return UserOut(
        id=int(info["id"]),
        username=str(info["username"]),
        created_at=str(info.get("created_at") or ""),
    )
