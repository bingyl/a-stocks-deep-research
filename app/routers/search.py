from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.logging import log_caught
from app.deps.auth import get_current_user
from app.models.search import StockSuggestItem, StockSuggestResponse
from app.models.sync import SyncResult
from app.services.stock import StockDataError
from app.services.sync_stocks import sync_stock_universe
from app.services.universe import (
    ensure_universe,
    reload_from_db,
    suggest_stocks,
    universe_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/search",
    tags=["search"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "/suggest",
    response_model=StockSuggestResponse,
    summary="股票联想：代码 / 名称 / 拼音首字母",
)
async def suggest(
    q: str = Query(..., min_length=1, max_length=32, description="关键词，如 网络 / 600519 / gzmt"),
    limit: int = Query(40, ge=1, le=200, description="返回条数上限"),
):
    try:
        await ensure_universe()
        items, total = await suggest_stocks(q, limit=limit)
        status = await universe_status()
        return StockSuggestResponse(
            query=q.strip(),
            total=total,
            items=[
                StockSuggestItem(
                    code=i.code,
                    name=i.name,
                    initials=i.initials,
                    market=i.market,
                    board=i.board,
                    industry=i.industry,
                )
                for i in items
            ],
            universe_ready=status["ready"],
            universe_count=status["count"],
        )
    except StockDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "股票联想失败 q=%s", q, exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=502, detail=f"联想失败: {exc}") from exc


@router.get("/universe/status", summary="股票池同步状态")
async def get_universe_status():
    return await universe_status()


@router.post(
    "/universe/sync",
    response_model=SyncResult,
    summary="手动同步股票池到业务库",
)
async def trigger_sync(
    full: bool = Query(False, description="是否按全量语义记录（逻辑同日常对比）"),
    refresh_industry: bool = Query(True, description="是否刷新行业映射"),
):
    try:
        result = await sync_stock_universe(
            full=full,
            refresh_industry=refresh_industry,
        )
        await reload_from_db()
        return SyncResult(**result)
    except StockDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "股票池同步失败", exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=502, detail=f"同步失败: {exc}") from exc
