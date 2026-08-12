"""ParentChild + RRF → LangChain BaseRetriever（包装现有 Chroma/Milvus 后端）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict, Field

from app.core.config import get_settings
from app.core.logging import log_caught
from app.rag.context import get_rag_context
from app.rag.freshness import age_hours, is_stale, summarize_freshness
from app.rag.util import preview_text
from app.persistence.vectorstore.factory import get_parent_child_index

logger = logging.getLogger(__name__)


def _error_docs(error: str) -> list[Document]:
    payload = {"ok": False, "error": error, "items": [], "requires_web_refresh": True}
    return [
        Document(
            page_content=json.dumps(payload, ensure_ascii=False),
            metadata={"kind": "summary", "ok": False},
        )
    ]


_FINANCE_QUERY_RE = re.compile(
    r"(财务|营收|收入|利润|净利|现金流|ROE|ROA|资产负债|毛利率|净利率|"
    r"PE|PB|估值|每股|业绩|财报|季报|年报|中报)",
    re.IGNORECASE,
)

_STRUCTURED_TOOLS = frozenset(
    {
        "get_stock_finance",
        "get_stock_overview",
        "get_stock_quote",
        "get_kline",
        "get_technical_analysis",
        "compare_board_fundamentals",
        "get_peer_valuation",
        "get_board_resonance",
    }
)
_FINANCE_TOOLS = frozenset({"get_stock_finance", "get_stock_overview"})
_WEB_TOOLS = frozenset(
    {
        "web_search",
        "search_company_news",
        "search_policy_impact",
        "search_macro_international",
    }
)


def _build_docs_from_hits(
    *,
    report_id: str,
    code: str,
    query: str,
    hits: list[Any],
    parents: list[Any],
    stale_hours: int,
) -> list[Document]:
    parent_by_id = {p.id: p for p in parents}
    compact_items: list[dict[str, Any]] = []
    metas_for_freshness: list[dict[str, Any]] = []
    hit_docs: list[Document] = []
    # 同一 parent 可能被多个 child 命中；送给模型前按 parent_id 保序去重（保留最高分）
    seen_parents: set[str] = set()

    for h in hits:
        meta = {
            k: v
            for k, v in (h.metadata or {}).items()
            if not str(k).startswith("_")
        }
        pid = str(meta.get("parent_id") or "")
        parent = parent_by_id.get(pid) if pid else None
        if parent and parent.metadata:
            for key in ("created_at", "created_at_ts", "source_published_at", "tool", "source_type"):
                if key not in meta and parent.metadata.get(key) is not None:
                    meta[key] = parent.metadata.get(key)

        hours = age_hours(meta)
        item_stale = is_stale(meta, stale_hours=stale_hours)
        metas_for_freshness.append(meta)
        parent_text = (parent.text if parent else "")[:4000]
        # summary 只留轻量索引，避免与正文重复烧 token
        compact_items.append(
            {
                "parent_id": pid or None,
                "child_id": h.id,
                "score": h.score,
                "tool": meta.get("tool"),
                "stale": item_stale,
                "age_hours": round(hours, 2) if hours is not None else None,
            }
        )

        dedupe_key = pid or f"child:{h.id}"
        if dedupe_key in seen_parents:
            continue
        seen_parents.add(dedupe_key)

        hit_docs.append(
            Document(
                page_content=(
                    f"tool={meta.get('tool') or ''}\n"
                    f"parent_id={pid}\n"
                    f"score={h.score}\n"
                    f"stale={item_stale}\n"
                    f"age_hours={compact_items[-1]['age_hours']}\n\n"
                    f"{parent_text}"
                ),
                metadata={
                    "kind": "hit",
                    "child_id": h.id,
                    "parent_id": pid,
                    "score": h.score,
                    "stale": item_stale,
                    "tool": str(meta.get("tool") or ""),
                    "age_hours": compact_items[-1]["age_hours"],
                    "created_at": meta.get("created_at") or "",
                },
            )
        )

    freshness = summarize_freshness(metas_for_freshness, stale_hours=stale_hours)
    hit_tools = {
        str((m or {}).get("tool") or "")
        for m in metas_for_freshness
        if (m or {}).get("tool")
    }
    structured_hits = bool(hit_tools & _STRUCTURED_TOOLS)
    finance_hits = bool(hit_tools & _FINANCE_TOOLS)
    web_hits = bool(hit_tools & _WEB_TOOLS)
    finance_query = bool(_FINANCE_QUERY_RE.search(query or ""))

    if freshness["requires_web_refresh"]:
        if finance_query:
            hint = (
                "知识库无可用未过期命中。财务问题请调用 get_stock_finance；"
                "新闻/公告问题再 web_search 或 search_company_news。"
            )
        elif structured_hits:
            hint = (
                "命中材料已全部过期。按问题类型选择：财务用 get_stock_finance，"
                "新闻用 web_search / search_company_news。"
            )
        else:
            hint = (
                f"知识库无可用未过期命中（阈值 {stale_hours} 小时）。"
                "财务类用 get_stock_finance；新闻类用 web_search。"
            )
    elif finance_query and not finance_hits:
        # 问财务却只召回新闻：禁止「直接作答」锁死错误材料
        hint = (
            "当前命中偏新闻/其它材料，未必含所需财务数字。"
            "请调用 get_stock_finance（或 get_stock_overview）补充后作答，勿仅凭新闻推断财报。"
        )
    elif freshness.get("fresh_count", 0) > 0 and finance_hits:
        hint = (
            "知识库已有未过期财务/概览材料，可直接据此回答；"
            "勿重复调用 get_stock_finance / get_stock_overview。"
            "仅用户明确要「实时最新价」时才可 get_stock_quote。"
        )
        if web_hits and freshness.get("stale_count", 0) > 0:
            hint += " 部分新闻可能过期，涉及时效舆情可再 web_search。"
    elif freshness.get("fresh_count", 0) > 0 and structured_hits:
        hint = (
            "知识库有未过期结构化材料，优先据此作答；"
            "仅当正文确实缺少所需字段时再补调对应工具。"
        )
    elif freshness.get("stale_count", 0) > 0:
        hint = (
            f"存在未过期命中可用；另有 {freshness['stale_count']} 条已过期，请忽略过期条。"
            "仅新闻/公告类主题才需要再 web_search。"
        )
    else:
        hint = "知识库材料仍在时效内，优先直接作答。"

    summary = {
        "ok": True,
        "report_id": report_id,
        "code": code,
        "query": query,
        "hit_count": len(hits),
        "parent_count": len(hit_docs),
        "items": compact_items,
        "freshness": freshness,
        "stale": freshness["stale"],
        "requires_web_refresh": freshness["requires_web_refresh"],
        "hint": hint,
    }
    summary_doc = Document(
        page_content=json.dumps(summary, ensure_ascii=False, default=str),
        metadata={
            "kind": "summary",
            "ok": True,
            "requires_web_refresh": freshness["requires_web_refresh"],
            "stale": freshness["stale"],
        },
    )
    return [summary_doc, *hit_docs]


class ReportKnowledgeRetriever(BaseRetriever):
    """按追问 ContextVar 的 report_id 做父子向量召回。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    top_k: int = Field(default=5, ge=1, le=12)

    def _resolve_scope(self) -> tuple[str | None, str, str | None]:
        """返回 (error, report_id, code)。error 非空表示不可检索。"""
        settings = get_settings()
        if not settings.rag_followup_enabled():
            return "未启用知识库召回（RAG_ENABLED / RAG_FOLLOWUP_TOOL）", "", None
        ctx = get_rag_context()
        if not ctx or not ctx.report_id:
            return "缺少报告上下文，无法限定召回范围", "", None
        return None, ctx.report_id, ctx.code

    def _search_sync(self, query: str, report_id: str) -> tuple[list, list]:
        settings = get_settings()
        top_k = max(1, min(int(self.top_k or 5), 12))
        idx = get_parent_child_index()
        hits = idx.search(
            settings.rag_collection,
            query,
            top_k=top_k,
            where={"report_id": report_id},
        )
        parents = idx.resolve_parents(settings.rag_collection, hits, dedupe=True)
        return hits, parents

    def _get_relevant_documents(
        self,
        query: str,
        *,
        run_manager: CallbackManagerForRetrieverRun,
    ) -> list[Document]:
        err, report_id, code = self._resolve_scope()
        if err:
            return _error_docs(err)
        q = (query or "").strip()
        if not q:
            return _error_docs("query 不能为空")

        settings = get_settings()
        stale_hours = max(0, int(settings.rag_stale_hours or 24))
        try:
            hits, parents = self._search_sync(q, report_id)
            docs = _build_docs_from_hits(
                report_id=report_id,
                code=code or "",
                query=q,
                hits=hits,
                parents=parents,
                stale_hours=stale_hours,
            )
            parent_n = sum(1 for d in docs if d.metadata.get("kind") == "hit")
            logger.info(
                "RAG 召回：报告#%s，查询「%s」，子命中 %s，去重后父文档 %s",
                report_id,
                preview_text(q, 120),
                len(hits),
                parent_n,
            )
            return docs
        except Exception as exc:
            log_caught(logger, "RAG 召回失败", exc=exc, level=logging.ERROR)
            return _error_docs(str(exc))

    async def _aget_relevant_documents(
        self,
        query: str,
        *,
        run_manager: AsyncCallbackManagerForRetrieverRun,
    ) -> list[Document]:
        err, report_id, code = self._resolve_scope()
        if err:
            return _error_docs(err)
        q = (query or "").strip()
        if not q:
            return _error_docs("query 不能为空")

        settings = get_settings()
        stale_hours = max(0, int(settings.rag_stale_hours or 24))
        try:
            hits, parents = await asyncio.to_thread(self._search_sync, q, report_id)
            docs = _build_docs_from_hits(
                report_id=report_id,
                code=code or "",
                query=q,
                hits=hits,
                parents=parents,
                stale_hours=stale_hours,
            )
            parent_n = sum(1 for d in docs if d.metadata.get("kind") == "hit")
            logger.info(
                "RAG 召回：报告#%s，查询「%s」，子命中 %s，去重后父文档 %s",
                report_id,
                preview_text(q, 120),
                len(hits),
                parent_n,
            )
            return docs
        except Exception as exc:
            log_caught(logger, "RAG 召回失败", exc=exc, level=logging.ERROR)
            return _error_docs(str(exc))
