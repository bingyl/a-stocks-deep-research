from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.services.stock import StockDataError
from app.services.sync_stocks import (
    count_listed,
    get_meta,
    latest_sync_log,
    load_listed_rows,
    sync_stock_universe,
)

# 内存索引热缓存；底层真相在业务库（sqlite/postgres）
MEMORY_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class StockMeta:
    code: str
    name: str
    name_norm: str
    pinyin: str
    initials: str
    market: str = ""
    board: str = ""
    industry: str = ""


_lock = threading.Lock()
_cache: list[StockMeta] = []
_loaded_at: float = 0.0
_loading = False


def _rows_to_items(rows: list[dict]) -> list[StockMeta]:
    items: list[StockMeta] = []
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        name = str(row.get("name") or "")
        if len(code) != 6 or not name:
            continue
        items.append(
            StockMeta(
                code=code,
                name=name,
                name_norm=str(row.get("name_norm") or name.lower()),
                pinyin=str(row.get("pinyin") or ""),
                initials=str(row.get("initials") or ""),
                market=str(row.get("market") or ""),
                board=str(row.get("board") or ""),
                industry=str(row.get("industry") or ""),
            )
        )
    return items


async def reload_from_db() -> list[StockMeta]:
    global _cache, _loaded_at
    items = _rows_to_items(await load_listed_rows())
    with _lock:
        _cache = items
        _loaded_at = time.time()
        return _cache


async def ensure_universe(force: bool = False) -> list[StockMeta]:
    """确保内存索引可用：优先读业务库；库空则同步一次。"""
    global _cache, _loaded_at, _loading

    with _lock:
        fresh = (
            not force
            and _cache
            and (time.time() - _loaded_at) < MEMORY_TTL_SECONDS
        )
        if fresh:
            return _cache
        if _loading:
            return _cache
        _loading = True

    try:
        if force or (await count_listed()) == 0:
            await sync_stock_universe(full=True, refresh_industry=True)
            return await reload_from_db()

        items = await reload_from_db()
        if not items:
            await sync_stock_universe(full=True, refresh_industry=True)
            items = await reload_from_db()
        if not items:
            raise StockDataError("本地股票库为空，同步后仍无数据")
        return items
    finally:
        with _lock:
            _loading = False


async def universe_status() -> dict:
    with _lock:
        mem_count = len(_cache)
        loading = _loading
        loaded_at = _loaded_at or None
    log = await latest_sync_log()
    listed = await count_listed()
    return {
        "ready": mem_count > 0 or listed > 0,
        "count": mem_count or listed,
        "loaded_at": loaded_at,
        "loading": loading,
        "db_listed": listed,
        "last_sync_at": await get_meta("last_sync_at"),
        "last_sync_kind": await get_meta("last_sync_kind"),
        "latest_sync": log,
    }


def _score(item: StockMeta, q: str, q_lower: str, is_ascii: bool) -> Optional[int]:
    code = item.code
    name = item.name
    name_norm = item.name_norm

    if q.isdigit():
        if code == q.zfill(len(q)) or code == q:
            return 0
        if code.startswith(q):
            return 10
        if q in code:
            return 20
        return None

    if q in name or q_lower in name_norm:
        if name.startswith(q) or name_norm.startswith(q_lower):
            return 30
        return 40

    # 行业 / 板块关键词（中文）
    if not is_ascii:
        if item.industry and q in item.industry:
            return 90
        if item.board and q in item.board:
            return 95
        return None

    initials = item.initials
    pinyin = item.pinyin
    if initials.startswith(q_lower):
        return 50
    if q_lower in initials:
        return 60
    if pinyin.startswith(q_lower):
        return 70
    if q_lower in pinyin:
        return 80
    return None


async def suggest_stocks(query: str, limit: int = 30) -> tuple[list[StockMeta], int]:
    q = (query or "").strip()
    if not q:
        return [], 0

    items = await ensure_universe()
    q_lower = q.lower()
    is_ascii = bool(re.fullmatch(r"[0-9A-Za-z.]+", q))

    code_q = q_lower
    code_q = re.sub(r"^(sh|sz|bj)\.?", "", code_q)
    code_q = code_q.replace(".sh", "").replace(".sz", "").replace(".bj", "")

    scored: list[tuple[int, StockMeta]] = []
    for item in items:
        query_for_score = code_q if (is_ascii and any(ch.isdigit() for ch in code_q)) else q
        score = _score(item, query_for_score, query_for_score.lower(), is_ascii)
        if score is None and query_for_score != q:
            score = _score(item, q, q_lower, is_ascii)
        if score is not None:
            scored.append((score, item))

    scored.sort(key=lambda x: (x[0], x[1].code))
    total = len(scored)
    limit = max(1, min(limit, 200))
    return [item for _, item in scored[:limit]], total
