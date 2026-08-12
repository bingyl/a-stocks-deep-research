from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.logging import log_caught
from app.deps.auth import get_current_user
from app.models.stock import FinanceOverview, QuoteInfo, StockOverview
from app.services.stock import (
    StockDataError,
    StockNotFoundError,
    fetch_finance,
    fetch_overview,
    fetch_quote,
    normalize_code,
)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/stock",
    tags=["stock"],
    dependencies=[Depends(get_current_user)],
)


@router.get(
    "/{code}",
    response_model=StockOverview,
    summary="综合查询：最新股价 + 财务概况",
)
async def get_stock_overview(
    code: str = Path(..., description="股票代码，如 600519 / sh600519 / 000001"),
):
    try:
        return await fetch_overview(code)
    except StockNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StockDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "综合查询失败 code=%s", code, exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=502, detail=f"查询失败: {exc}") from exc


@router.get(
    "/{code}/quote",
    response_model=QuoteInfo,
    summary="最新股价与行情",
)
async def get_stock_quote(
    code: str = Path(..., description="股票代码"),
):
    try:
        return await fetch_quote(code)
    except StockNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "行情获取失败 code=%s", code, exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=502, detail=f"行情获取失败: {exc}") from exc


@router.get(
    "/{code}/finance",
    response_model=FinanceOverview,
    summary="财务指标与报表摘要",
)
async def get_stock_finance(
    code: str = Path(..., description="股票代码"),
    with_name: bool = Query(True, description="是否顺带拉取名称"),
):
    try:
        name = None
        if with_name:
            quote = await fetch_quote(code)
            name = quote.name
            code = quote.code
        else:
            code = normalize_code(code)
        return await asyncio.to_thread(fetch_finance, code, name)
    except StockNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StockDataError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "财务获取失败 code=%s", code, exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=502, detail=f"财务获取失败: {exc}") from exc
