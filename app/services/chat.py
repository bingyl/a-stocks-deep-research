"""追问对话历史：存放在 LangGraph DocStore（namespace=chat/{report_id}）。"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from app.persistence.docstore import get_doc_store

logger = logging.getLogger(__name__)

REFUSAL_TEXT = (
    "抱歉，我只能围绕该股的深研报告、财务、估值、行业与相关市场信息作答。"
    "您的问题与投研无关，请换一个与这只股票或报告内容相关的问题。"
)

_SEQ_KEY = "_seq"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def chat_namespace(report_id: int) -> tuple[str, ...]:
    return ("chat", str(int(report_id)))


def _message_from_item(item: Any, report_id: int) -> dict[str, Any] | None:
    if item is None or item.key == _SEQ_KEY:
        return None
    try:
        mid = int(item.key)
    except (TypeError, ValueError):
        # 旁路库脏键（非数字）直接丢弃，避免列表接口 ResponseValidationError
        return None
    val = item.value or {}
    role = str(val.get("role") or "").strip()
    content = str(val.get("content") or "")
    if not role and not content:
        return None
    return {
        "id": mid,
        "report_id": int(report_id),
        "role": role,
        "content": content,
        "refused": bool(val.get("refused")),
        "model": val.get("model") or "",
        "created_at": val.get("created_at") or "",
    }


def _next_message_id(_report_id: int) -> int:
    """跨进程唯一、近似时间序的正整数 id（≤ JS Number.MAX_SAFE_INTEGER）。"""
    # 42-bit ms（相对自定义纪元）+ 11-bit random = 53 bit，JSON/前端可安全解析
    epoch_ms = 1_700_000_000_000  # ~2023-11-14
    ms = max(0, int(time.time() * 1000) - epoch_ms) & ((1 << 42) - 1)
    return (ms << 11) | secrets.randbelow(1 << 11)


def count_messages(report_id: int) -> int:
    store = get_doc_store()
    items = store.search(chat_namespace(report_id), limit=10_000)
    return sum(1 for it in items if it.key != _SEQ_KEY)


def count_messages_by_report_ids(report_ids: list[int]) -> dict[int, int]:
    if not report_ids:
        return {}
    return {int(rid): count_messages(int(rid)) for rid in report_ids}


def list_messages(report_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    """返回最近 limit 条（时间升序）。"""
    limit = max(1, min(int(limit or 200), 500))
    store = get_doc_store()
    items = store.search(chat_namespace(report_id), limit=10_000)
    rows: list[dict[str, Any]] = []
    for it in items:
        msg = _message_from_item(it, report_id)
        if msg:
            rows.append(msg)
    rows.sort(
        key=lambda m: (
            m.get("created_at") or "",
            int(m["id"]) if isinstance(m["id"], int) else str(m["id"]),
        )
    )
    return rows[-limit:]


def recent_messages(report_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    """最近 N 条，按时间正序返回（供拼上下文）。"""
    limit = max(1, min(int(limit or 20), 50))
    return list_messages(report_id, limit=limit)


def add_message(
    *,
    report_id: int,
    role: str,
    content: str,
    refused: bool = False,
    model: str = "",
) -> dict[str, Any]:
    role_n = (role or "").strip().lower()
    if role_n not in {"user", "assistant", "system"}:
        raise ValueError("role 无效")
    text = (content or "").strip()
    if not text:
        raise ValueError("content 不能为空")
    created_at = _now_iso()
    msg_id = _next_message_id(int(report_id))
    store = get_doc_store()
    store.put(
        chat_namespace(report_id),
        str(msg_id),
        {
            "role": role_n,
            "content": text,
            "refused": bool(refused),
            "model": model or "",
            "created_at": created_at,
            "report_id": int(report_id),
        },
    )
    return {
        "id": msg_id,
        "report_id": int(report_id),
        "role": role_n,
        "content": text,
        "refused": bool(refused),
        "model": model or "",
        "created_at": created_at,
    }


def delete_messages(report_id: int) -> int:
    store = get_doc_store()
    ns = chat_namespace(report_id)
    items = store.search(ns, limit=10_000)
    n = 0
    for it in items:
        store.delete(ns, it.key)
        n += 1
    return n


def delete_messages_for_report(report_id: int) -> None:
    """删除报告对话（供 reports.delete_report 调用）。"""
    delete_messages(int(report_id))
