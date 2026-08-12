"""按 report_id + tool + 参数查找知识库是否已有未过期父文档。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import get_settings
from app.rag.chunking import stable_id
from app.rag.freshness import age_hours, is_stale
from app.persistence.vectorstore.factory import get_parent_child_index
from app.persistence.vectorstore.types import ParentDocument

logger = logging.getLogger(__name__)

# 追问时：这些工具若知识库已有「同参数」未过期结果，应跳过重复拉取
# （不含 get_stock_profile：该工具不入库，短路永远命中不了）
FOLLOWUP_DEDUP_TOOLS = frozenset(
    {
        "get_stock_finance",
        "get_stock_overview",
        "get_kline",
        "get_technical_analysis",
        "get_industry_peers",
        "get_peer_valuation",
        "get_board_resonance",
        "get_board_members",
        "compare_board_fundamentals",
    }
)


def tool_arg_key(arguments: dict[str, Any] | None) -> str:
    return stable_id(
        json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True, default=str)
    )


def tool_parent_prefix(report_id: str, tool: str, arguments: dict[str, Any] | None) -> str:
    return f"{report_id}:{tool}:{tool_arg_key(arguments)}:"


def list_ids_with_prefix(namespace: str, prefix: str) -> list[str]:
    """列出以 prefix 开头的父文档 id（分页枚举，避免 newest-N 截断）。"""
    store = get_parent_child_index().doc_store
    return store.list_ids(namespace, key_prefix=prefix)


def list_tool_parents(
    report_id: str,
    tool: str,
    arguments: dict[str, Any] | None,
) -> list[ParentDocument]:
    """仅精确匹配同参数前缀；避免不同 period/limit 被误短路。"""
    if not report_id or not tool:
        return []
    settings = get_settings()
    collection = settings.rag_collection
    prefix = tool_parent_prefix(report_id, tool, arguments)
    ids = list_ids_with_prefix(collection, prefix)
    if not ids:
        return []
    return get_parent_child_index().doc_store.get(collection, ids)


def fresh_tool_parents(
    report_id: str,
    tool: str,
    arguments: dict[str, Any] | None,
    *,
    stale_hours: int | None = None,
) -> list[ParentDocument]:
    """返回未过期的父文档；若全部过期则空列表。"""
    parents = list_tool_parents(report_id, tool, arguments)
    if not parents:
        return []
    hours = (
        stale_hours
        if stale_hours is not None
        else max(0, int(get_settings().rag_stale_hours or 24))
    )
    return [p for p in parents if not is_stale(p.metadata, stale_hours=hours)]


def skipped_tool_payload(
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    parents: list[ParentDocument],
) -> dict[str, Any]:
    ages = [age_hours(p.metadata) for p in parents]
    ages_f = [a for a in ages if a is not None]
    code = str((arguments or {}).get("code") or "")
    return {
        "skipped": True,
        "reason": (
            "本报告知识库已有该工具未过期结果，已跳过重复拉取。"
            "请改用 rag_search 检索相关关键词（如现金流、营业收入、ROE）直接作答，"
            "不要再次调用同类财务/行情结构化工具。"
        ),
        "tool": tool,
        "code": code,
        "parent_count": len(parents),
        "age_hours_min": round(min(ages_f), 3) if ages_f else None,
        "suggest_query": f"{code} 财务 现金流".strip(),
    }


def parents_content_unchanged(parents: list[ParentDocument]) -> bool:
    """待写入父文档是否已在库中且正文一致（用于跳过重复向量化）。"""
    if not parents:
        return False
    settings = get_settings()
    collection = settings.rag_collection
    existing = get_parent_child_index().doc_store.get(
        collection, [p.id for p in parents]
    )
    if len(existing) < len(parents):
        return False
    by_id = {e.id: e for e in existing}
    for p in parents:
        old = by_id.get(p.id)
        if old is None or (old.text or "") != (p.text or ""):
            return False
    return True
