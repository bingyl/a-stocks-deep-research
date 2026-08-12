from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langchain_core.tools import tool

from app.core.logging import log_caught
from app.persistence.db import init_db, async_session_scope
from app.persistence.db.models import Stock
from app.integrations.bocha import bocha_web_search
from app.services.boards import (
    analyze_board_resonance,
    compare_board_fundamentals as compare_board_fundamentals_svc,
    fetch_board_member_snapshot,
)
from app.services.market import (
    fetch_kline,
    fetch_peer_valuation,
    fetch_technical_snapshot,
    find_industry_peers,
)
from app.services.stock import fetch_finance, fetch_overview, fetch_quote, normalize_code

logger = logging.getLogger(__name__)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _tool_fail(tool_name: str, exc: BaseException) -> str:
    log_caught(logger, "%s failed", tool_name, exc=exc, level=logging.ERROR)
    return _dump({"error": f"{type(exc).__name__}: {exc}"})


async def _get_profile(code: str) -> dict[str, Any]:
    code = normalize_code(code)
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(Stock, code)
        if not row:
            return {"code": code, "found": False, "message": "本地库未找到该股票"}
        return {
            "found": True,
            "code": row.code,
            "name": row.name,
            "market": row.market,
            "board": row.board,
            "industry": row.industry,
            "status": row.status,
            "pinyin": row.pinyin,
            "initials": row.initials,
        }


# ---------- 情报搜集智能体工具（仅联网/政策舆情） ----------


@tool
async def web_search(
    query: str,
    count: int = 8,
    freshness: str = "oneYear",
) -> str:
    """博查全网搜索。适合公司新闻、竞争格局、风险事件、机构观点等。

    Args:
        query: 搜索关键词，建议包含公司名/代码与议题
        count: 返回条数 1-20
        freshness: noLimit/oneDay/oneWeek/oneMonth/oneYear
    """
    try:
        return _dump(await bocha_web_search(query, count=count, freshness=freshness))
    except Exception as exc:
        return _tool_fail("web_search", exc)


@tool
async def search_policy_impact(topic: str, count: int = 6) -> str:
    """检索国家政策 / 监管 / 产业规划对某主题的影响（博查）。

    Args:
        topic: 如「白酒消费税」「半导体设备国产替代」「新能源补贴」
        count: 返回条数
    """
    query = f"{topic} 政策 监管 影响 最新"
    try:
        return _dump(
            await bocha_web_search(query, count=count, freshness="oneYear")
        )
    except Exception as exc:
        return _tool_fail("search_policy_impact", exc)


@tool
async def search_macro_international(topic: str, count: int = 6) -> str:
    """检索国际形势 / 海外市场 / 地缘与大宗商品对主题的影响。

    Args:
        topic: 如「美联储降息 白酒」「中美科技摩擦 半导体」
        count: 返回条数
    """
    query = f"{topic} 国际形势 海外 影响"
    try:
        return _dump(
            await bocha_web_search(query, count=count, freshness="oneYear")
        )
    except Exception as exc:
        return _tool_fail("search_macro_international", exc)


@tool
async def search_company_news(
    company: str,
    code: str = "",
    count: int = 8,
    freshness: str = "oneMonth",
) -> str:
    """搜索该股最新动态：公司公告、新闻、业绩说明、调研与舆情。

    Args:
        company: 公司名称，如「民生银行」
        code: 股票代码，可选，有助于提高命中率
        count: 返回条数 1-20
        freshness: oneDay/oneWeek/oneMonth/oneYear
    """
    code = (code or "").strip()
    company = (company or "").strip()
    query = f"{company} {code} 公告 新闻 最新动态".strip()
    try:
        return _dump(
            await bocha_web_search(query, count=count, freshness=freshness)
        )
    except Exception as exc:
        return _tool_fail("search_company_news", exc)


INTEL_TOOLS = [
    web_search,
    search_company_news,
    search_policy_impact,
    search_macro_international,
]


# ---------- 市场分析智能体工具（本地行情/财务/K线/同业） ----------


@tool
async def get_stock_profile(code: str) -> str:
    """查询本地股票池静态档案：代码、名称、市场、板块、行业。"""
    try:
        return _dump(await _get_profile(code))
    except Exception as exc:
        return _tool_fail("get_stock_profile", exc)


@tool
async def get_stock_quote(code: str) -> str:
    """获取最新行情：现价、涨跌幅、PE/PB、市值、换手率等。"""
    try:
        return _dump((await fetch_quote(code)).model_dump())
    except Exception as exc:
        return _tool_fail("get_stock_quote", exc)


@tool
async def get_stock_finance(code: str) -> str:
    """获取财务概况：盈利、偿债、成长、现金流，同比/环比趋势，以及业绩预告。

    trends 中每条含 label；income_summary / earnings_forecasts 里 is_forecast=true 为预告区间（非正式报表），须标明预告并引用正式披露预约日。
    """
    try:
        code = normalize_code(code)
        profile = await _get_profile(code)
        name = profile.get("name") if profile.get("found") else None
        if not name:
            try:
                name = (await fetch_quote(code)).name
            except Exception as exc:
                log_caught(logger, "get_stock_finance 取名失败 code=%s", code, exc=exc)
                name = None
        fin = await asyncio.to_thread(fetch_finance, code, name)
        return _dump(fin.model_dump())
    except Exception as exc:
        return _tool_fail("get_stock_finance", exc)


@tool
async def get_stock_overview(code: str) -> str:
    """一次性获取行情 + 财务综合数据（含 earnings_forecasts 业绩预告，任意股票有则返回）。"""
    try:
        return _dump((await fetch_overview(code)).model_dump())
    except Exception as exc:
        return _tool_fail("get_stock_overview", exc)


@tool
async def get_kline(code: str, period: str = "daily", limit: int = 120) -> str:
    """获取历史K线（OHLCV）。period=daily/weekly/monthly。"""
    try:
        data = await fetch_kline(code, period=period, limit=limit)
        # 控制 token：只返回尾部 bars
        data["bars"] = data.get("bars", [])[-60:]
        return _dump(data)
    except Exception as exc:
        return _tool_fail("get_kline", exc)


@tool
async def get_technical_analysis(code: str) -> str:
    """K线技术面快照：均线、涨跌幅、波动、趋势偏向及近30日K线。"""
    try:
        return _dump(await fetch_technical_snapshot(code))
    except Exception as exc:
        return _tool_fail("get_technical_analysis", exc)


@tool
async def get_industry_peers(code: str, limit: int = 12) -> str:
    """查询同行业成分股列表（本地股票池，用于板块联动/同行对比）。"""
    try:
        return _dump(await find_industry_peers(code, limit))
    except Exception as exc:
        return _tool_fail("get_industry_peers", exc)


@tool
async def get_peer_valuation(code: str, limit: int = 8) -> str:
    """同业综合估值对比：同行 PE/PB/市值，并附 summary 分位与中位数。

    请结合 summary 与财务增速/ROE 做综合判断，不要只根据 PE 或 PB 单一指标下结论。
    """
    try:
        return _dump(await fetch_peer_valuation(code, limit=limit))
    except Exception as exc:
        return _tool_fail("get_peer_valuation", exc)


@tool
async def get_board_resonance(code: str) -> str:
    """查询个股所属行业板块，并与行业板块当日涨跌做联动分析（不含概念题材）。

    返回 industry、primary_industry_board，以及 summary.resonance（偏多/偏空联动或背离）。
    """
    try:
        return _dump(await analyze_board_resonance(code))
    except Exception as exc:
        return _tool_fail("get_board_resonance", exc)


@tool
async def get_board_members(
    board_code: str,
    kind: str = "industry",
    limit: int = 16,
    target_code: str = "",
) -> str:
    """拉取某行业板块的成分股行情快照（涨跌、PE/PB、市值）。同业财务对比请优先用 compare_board_fundamentals。

    Args:
        board_code: 板块代码，如 BK0438
        kind: 仅建议 industry
        limit: 返回成分数量上限
        target_code: 可选，分析标的代码；命中时会标记 is_target=true
    """
    try:
        data = await asyncio.to_thread(
            fetch_board_member_snapshot,
            board_code,
            kind=kind,
            limit=limit,
            target_code=target_code or None,
        )
        return _dump(data)
    except Exception as exc:
        return _tool_fail("get_board_members", exc)


@tool
async def compare_board_fundamentals(
    code: str,
    peer_limit: int = 6,
    board_code: str = "",
) -> str:
    """行业板块内同业基本面横向对比（推荐主工具）。

    流程：取主行业板块成分股 → 拉取各股行情与财务 → 对比业绩(收入/利润及同比)、PE、PB、经营现金流、ROE。
    peer_limit 建议 5-8（含标的）；board_code 可指定行业板块，留空则自动选最细分行业板。
    """
    try:
        return _dump(
            await compare_board_fundamentals_svc(
                code,
                peer_limit=peer_limit,
                board_code=board_code or None,
            )
        )
    except Exception as exc:
        return _tool_fail("compare_board_fundamentals", exc)


ANALYST_TOOLS = [
    get_stock_profile,
    get_stock_quote,
    get_stock_finance,
    get_stock_overview,
    get_kline,
    get_technical_analysis,
    get_industry_peers,
    get_peer_valuation,
    get_board_resonance,
    get_board_members,
    compare_board_fundamentals,
]


TOOL_STEP_LABELS = {
    "task": "调度六维分析",
    "web_search": "联网搜索情报",
    "search_company_news": "检索公司公告与新闻",
    "search_policy_impact": "检索政策影响",
    "search_macro_international": "检索国际形势影响",
    "get_stock_profile": "读取股票档案",
    "get_stock_quote": "拉取最新行情",
    "get_stock_finance": "拉取财务数据",
    "get_stock_overview": "拉取综合概览",
    "get_kline": "拉取历史K线",
    "get_technical_analysis": "计算技术面指标",
    "get_industry_peers": "查询同行业成分",
    "get_peer_valuation": "对比同业估值",
    "get_board_resonance": "行业板块联动",
    "get_board_members": "拉取板块成分股",
    "compare_board_fundamentals": "板块成分财务对比",
    "rag_search": "知识库召回",
}


def _tool_label(name: str) -> str:
    return TOOL_STEP_LABELS.get(name, f"调用工具 {name}")


def _short(text: Any, n: int = 40) -> str:
    s = str(text or "").replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"
