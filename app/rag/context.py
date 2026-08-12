"""RAG / 工具调用上下文（避免改每个 @tool 签名）。

使用 ContextVar 做请求隔离；不再使用进程级单一 fallback（并发会串 report_id）。
asyncio.to_thread / 支持 context 传播的执行器可读到同一 ContextVar。
"""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_rag_ctx: ContextVar["RagContext | None"] = ContextVar("rag_ctx", default=None)
_rag_token: ContextVar[str | None] = ContextVar("rag_token", default=None)

# token -> ctx：仅用于同 token 的显式查找；clear 只删自己的 token，不误伤其他请求
_store_lock = threading.Lock()
_ctx_by_token: dict[str, RagContext] = {}


@dataclass(slots=True)
class RagContext:
    report_id: str
    code: str = ""
    name: str = ""
    stage: str = ""
    token: str = ""


def set_rag_context(
    report_id: str,
    *,
    code: str = "",
    name: str = "",
    stage: str = "",
) -> str:
    """设置当前任务上下文，返回 token（供测试或显式清理）。"""
    # 同任务内重复 set：替换旧 token，避免泄漏
    old = _rag_token.get()
    if old:
        with _store_lock:
            _ctx_by_token.pop(old, None)

    token = uuid.uuid4().hex
    ctx = RagContext(
        report_id=str(report_id),
        code=code or "",
        name=name or "",
        stage=stage or "",
        token=token,
    )
    _rag_ctx.set(ctx)
    _rag_token.set(token)
    with _store_lock:
        _ctx_by_token[token] = ctx
    return token


def update_rag_stage(stage: str) -> None:
    cur = get_rag_context()
    if cur is None:
        return
    set_rag_context(
        cur.report_id, code=cur.code, name=cur.name, stage=stage or ""
    )


def get_rag_context() -> RagContext | None:
    ctx = _rag_ctx.get()
    if ctx is not None:
        return ctx
    token = _rag_token.get()
    if not token:
        return None
    with _store_lock:
        return _ctx_by_token.get(token)


def clear_rag_context() -> None:
    """只清理当前 ContextVar 对应的 token，不影响其他并发请求。"""
    token = _rag_token.get()
    _rag_ctx.set(None)
    _rag_token.set(None)
    if token:
        with _store_lock:
            _ctx_by_token.pop(token, None)
