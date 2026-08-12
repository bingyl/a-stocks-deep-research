from __future__ import annotations

import logging
import time
from typing import Any, Optional

import akshare as ak

from app.core.logging import log_caught
from app.persistence.base import dialect_name, masked_database_url
from app.persistence.db import init_db, async_session_scope, async_upsert_sync_meta
from app.persistence.db.models import Stock, SyncLog, SyncMeta
from app.services.stock import StockDataError
from app.services.stock_meta import (
    clean_name,
    infer_market_board,
    normalize_code,
    to_pinyin,
    utc_now_iso,
)
from sqlalchemy import func, select

logger = logging.getLogger(__name__)


def fetch_remote_listed() -> dict[str, dict[str, str]]:
    """拉取当前在市 A 股代码与名称。"""
    try:
        df = ak.stock_info_a_code_name()
    except Exception as exc:
        log_caught(logger, "股票列表获取失败", exc=exc, level=logging.ERROR)
        raise StockDataError(f"股票列表获取失败: {exc}") from exc
    if df is None or df.empty:
        raise StockDataError("股票列表为空")

    result: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        code = normalize_code(row.get("code"))
        name = clean_name(row.get("name", ""))
        if not code or not name:
            continue
        market, board = infer_market_board(code)
        pinyin, initials = to_pinyin(name)
        result[code] = {
            "code": code,
            "name": name,
            "name_norm": name.lower(),
            "pinyin": pinyin,
            "initials": initials,
            "market": market,
            "board": board,
        }
    if not result:
        raise StockDataError("股票列表解析为空")
    return result


def fetch_delist_dates() -> dict[str, str]:
    """上交所/深交所退市（含暂停上市）日期，尽力而为。"""
    dates: dict[str, str] = {}

    try:
        sh = ak.stock_info_sh_delist()
        if sh is not None and not sh.empty:
            for _, row in sh.iterrows():
                code = normalize_code(row.get("公司代码"))
                if not code:
                    continue
                day = str(row.get("暂停上市日期") or row.get("终止上市日期") or "").strip()
                if day and day.lower() != "nan":
                    dates[code] = day
    except Exception as exc:
        log_caught(logger, "上交所退市列表获取失败", exc=exc)

    try:
        sz = ak.stock_info_sz_delist()
        if sz is not None and not sz.empty:
            for _, row in sz.iterrows():
                code = normalize_code(row.get("证券代码"))
                if not code:
                    continue
                day = str(row.get("终止上市日期") or "").strip()
                if day and day.lower() != "nan":
                    dates[code] = day
    except Exception as exc:
        log_caught(logger, "深交所退市列表获取失败", exc=exc)

    return dates


def fetch_industry_map(max_retries: int = 2) -> dict[str, str]:
    """行业板块成分 -> code: industry。东财不稳定时允许失败。"""
    mapping: dict[str, str] = {}
    boards = None
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            boards = ak.stock_board_industry_name_em()
            break
        except Exception as exc:
            last_exc = exc
            logger.warning("行业板块列表第 %s 次失败: %s", attempt, exc)
            time.sleep(1.2 * attempt)
    if boards is None or boards.empty:
        if last_exc:
            logger.warning("跳过行业同步: %s", last_exc)
        return mapping

    name_col = "板块名称" if "板块名称" in boards.columns else boards.columns[0]
    for board_name in boards[name_col].astype(str).tolist():
        board_name = board_name.strip()
        if not board_name:
            continue
        cons = None
        for attempt in range(1, max_retries + 1):
            try:
                cons = ak.stock_board_industry_cons_em(symbol=board_name)
                break
            except Exception as exc:
                log_caught(
                    logger,
                    "行业 %s 成分第 %s 次失败",
                    board_name,
                    attempt,
                    exc=exc,
                    level=logging.DEBUG,
                )
                time.sleep(0.4 * attempt)
        if cons is None or cons.empty:
            continue
        code_col = "代码" if "代码" in cons.columns else None
        if not code_col:
            continue
        for raw in cons[code_col].tolist():
            code = normalize_code(raw)
            if code:
                mapping[code] = board_name
    return mapping


async def get_meta(key: str, default: str = "") -> str:
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(SyncMeta, key)
        return str(row.value) if row else default


async def count_listed() -> int:
    await init_db()
    async with async_session_scope() as session:
        n = await session.scalar(
            select(func.count()).select_from(Stock).where(Stock.status == "listed")
        )
        return int(n or 0)


async def load_listed_rows() -> list[dict[str, Any]]:
    await init_db()
    async with async_session_scope() as session:
        rows = (await session.scalars(
            select(Stock).where(Stock.status == "listed").order_by(Stock.code)
        )).all()
        return [
            {
                "code": r.code,
                "name": r.name,
                "name_norm": r.name_norm,
                "pinyin": r.pinyin,
                "initials": r.initials,
                "market": r.market,
                "board": r.board,
                "industry": r.industry,
                "status": r.status,
            }
            for r in rows
        ]


async def latest_sync_log() -> Optional[dict[str, Any]]:
    await init_db()
    async with async_session_scope() as session:
        row = (await session.scalars(
            select(SyncLog).order_by(SyncLog.id.desc()).limit(1)
        )).first()
        if not row:
            return None
        return {
            "kind": row.kind,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "added": row.added,
            "updated": row.updated,
            "delisted": row.delisted,
            "industry_filled": row.industry_filled,
            "ok": row.ok,
            "message": row.message,
        }


async def sync_stock_universe(*, full: bool = False, refresh_industry: bool = True) -> dict[str, Any]:
    """
    全量/日常同步：
    - 远端有、本地无 -> 新上市
    - 远端有、本地有 -> 更新名称/拼音等；若曾退市则恢复
    - 本地在市、远端无 -> 标记退市
    - 可选补全行业
    """
    await init_db()
    kind = "full" if full else "daily"
    started = utc_now_iso()
    added = updated = delisted_n = industry_filled = 0
    message = "ok"
    db_dialect = dialect_name()
    db_url = masked_database_url()

    try:
        remote = fetch_remote_listed()
        delist_dates = fetch_delist_dates()
        industry_map: dict[str, str] = {}
        if refresh_industry:
            industry_map = fetch_industry_map()

        now = utc_now_iso()
        async with async_session_scope() as session:
            local_rows = {
                str(r.code): r for r in (await session.scalars(select(Stock))).all()
            }

            for code, info in remote.items():
                industry = industry_map.get(code, "")
                local = local_rows.get(code)
                if local is None:
                    session.add(
                        Stock(
                            code=code,
                            name=info["name"],
                            name_norm=info["name_norm"],
                            pinyin=info["pinyin"],
                            initials=info["initials"],
                            market=info["market"],
                            board=info["board"],
                            industry=industry,
                            status="listed",
                            list_date=None,
                            delist_date=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    added += 1
                    if industry:
                        industry_filled += 1
                    continue

                new_industry = industry or local.industry or ""
                old_industry = local.industry or ""
                changed = (
                    local.name != info["name"]
                    or local.pinyin != info["pinyin"]
                    or local.initials != info["initials"]
                    or local.market != info["market"]
                    or local.board != info["board"]
                    or local.status != "listed"
                    or (industry and old_industry != industry)
                )
                if changed:
                    local.name = info["name"]
                    local.name_norm = info["name_norm"]
                    local.pinyin = info["pinyin"]
                    local.initials = info["initials"]
                    local.market = info["market"]
                    local.board = info["board"]
                    local.industry = new_industry
                    local.status = "listed"
                    local.delist_date = None
                    local.updated_at = now
                    updated += 1
                    if industry and old_industry != industry:
                        industry_filled += 1
                elif industry and not old_industry:
                    local.industry = industry
                    local.updated_at = now
                    industry_filled += 1

            remote_codes = set(remote)
            for code, local in local_rows.items():
                if local.status != "listed":
                    continue
                if code in remote_codes:
                    continue
                day = delist_dates.get(code) or now[:10]
                local.status = "delisted"
                local.delist_date = day
                local.updated_at = now
                delisted_n += 1

            for code, day in delist_dates.items():
                if code in remote_codes:
                    continue
                local = local_rows.get(code)
                if local is None or local.status != "listed":
                    continue
                if not local.delist_date:
                    local.delist_date = day
                local.status = "delisted"
                local.updated_at = now

            await async_upsert_sync_meta(session, "last_sync_at", now, utc_now_iso())
            await async_upsert_sync_meta(session, "last_sync_kind", kind, utc_now_iso())
            await async_upsert_sync_meta(session, "listed_count", str(len(remote)), utc_now_iso())
            await async_upsert_sync_meta(session, "database_url", db_url, utc_now_iso())
            await async_upsert_sync_meta(session, "db_dialect", db_dialect, utc_now_iso())

            finished = utc_now_iso()
            session.add(
                SyncLog(
                    kind=kind,
                    started_at=started,
                    finished_at=finished,
                    added=added,
                    updated=updated,
                    delisted=delisted_n,
                    industry_filled=industry_filled,
                    ok=1,
                    message=message,
                )
            )

        return {
            "ok": True,
            "kind": kind,
            "added": added,
            "updated": updated,
            "delisted": delisted_n,
            "industry_filled": industry_filled,
            "listed_count": len(remote),
            "database_url": db_url,
            "db_dialect": db_dialect,
            "db_path": db_url,
            "started_at": started,
            "finished_at": utc_now_iso(),
            "message": message,
        }
    except Exception as exc:
        message = str(exc)
        log_caught(logger, "股票池同步失败", exc=exc, level=logging.ERROR)
        async with async_session_scope() as session:
            session.add(
                SyncLog(
                    kind=kind,
                    started_at=started,
                    finished_at=utc_now_iso(),
                    added=added,
                    updated=updated,
                    delisted=delisted_n,
                    industry_filled=industry_filled,
                    ok=0,
                    message=message,
                )
            )
        raise
