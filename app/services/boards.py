"""个股所属行业/概念板块 + 涨跌联动分析。

复用 app.extensions.stocks 下东财 F10 / 板块列表接口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from app.core.logging import log_caught
from app.extensions.stocks import (
    DEFAULT_DATA_DIR,
    fetch_board_list,
    fetch_board_members,
    lookup,
    normalize_board_code,
)
from app.services.stock import fetch_finance, fetch_quote, normalize_code

logger = logging.getLogger(__name__)

_QUOTE_CACHE_TTL = 30 * 60  # 板块行情缓存 30 分钟
_STYLE_KEYWORDS = (
    "沪股通",
    "深股通",
    "融资融券",
    "HS300",
    "沪深300",
    "上证50",
    "上证180",
    "中证",
    "MSCI",
    "富时",
    "标普",
    "标准普尔",
    "证金",
    "机构重仓",
    "大盘股",
    "小盘股",
    "中盘股",
    "权重股",
    "百元股",
    "消费风格",
    "价值风格",
    "成长风格",
    "行业龙头",
    "央视50",
    "创业板综",
    "科创板综",
)


def _to_float(v: Any) -> float | None:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_style_or_index(name: str) -> bool:
    n = name or ""
    return any(k in n for k in _STYLE_KEYWORDS)


def _direction(chg: float | None, eps: float = 0.05) -> str:
    if chg is None:
        return "未知"
    if chg > eps:
        return "上涨"
    if chg < -eps:
        return "下跌"
    return "平盘"


def _relative_label(stock_chg: float | None, board_chg: float | None) -> str:
    if stock_chg is None or board_chg is None:
        return "数据不足"
    spread = stock_chg - board_chg
    if abs(spread) < 0.3:
        return "与板块同步"
    if spread > 0:
        return "强于板块"
    return "弱于板块"


def _align_label(stock_chg: float | None, board_chg: float | None) -> str:
    if stock_chg is None or board_chg is None:
        return "未知"
    sd, bd = _direction(stock_chg), _direction(board_chg)
    if sd == "未知" or bd == "未知":
        return "未知"
    if sd == "平盘" and bd == "平盘":
        return "同向"
    if sd == "平盘" or bd == "平盘":
        # 一方平盘：用涨跌符号弱判断
        if stock_chg is None or board_chg is None:
            return "中性"
        if stock_chg == 0 or board_chg == 0:
            return "中性"
        return "同向" if (stock_chg > 0) == (board_chg > 0) else "背离"
    return "同向" if sd == bd else "背离"


def load_board_quote_maps(*, refresh: bool = False) -> dict[str, dict[str, Any]]:
    """返回 {BKxxxx: {name, kind, change_pct, up_count, down_count, leader, leader_chg}}。"""
    data_dir = Path(DEFAULT_DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path = data_dir / "board_quotes_cache.json"
    if cache_path.exists() and not refresh:
        age = time.time() - cache_path.stat().st_mtime
        if age < _QUOTE_CACHE_TTL:
            try:
                return json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                log_caught(logger, "板块行情缓存损坏，将重新拉取 path=%s", cache_path, exc=exc)

    out: dict[str, dict[str, Any]] = {}
    for kind in ("industry", "concept"):
        df = fetch_board_list(kind, retries=3, retry_delay=1.2)
        for _, r in df.iterrows():
            code = normalize_board_code(r["板块代码"])
            out[code] = {
                "board_code": code,
                "board_name": str(r["板块名称"] or "").strip(),
                "kind": kind,
                "change_pct": _to_float(r.get("涨跌幅")),
                "up_count": r.get("上涨家数"),
                "down_count": r.get("下跌家数"),
                "leader": str(r.get("领涨股票") or "").strip(),
                "leader_chg": _to_float(r.get("领涨股票-涨跌幅")),
            }
    cache_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def lookup_stock_boards(code_or_name: str, *, refresh_maps: bool = False) -> dict[str, Any]:
    """查询个股所属行业/概念（东财 F10）。"""
    return lookup(
        str(code_or_name).strip(),
        data_dir=Path(DEFAULT_DATA_DIR),
        refresh_maps=refresh_maps,
    )


def fetch_board_member_snapshot(
    board_code: str,
    *,
    kind: str = "industry",
    limit: int = 20,
    target_code: str | None = None,
) -> dict[str, Any]:
    """拉取板块成分股行情/估值快照（东财 clist），用于同业联动对比。"""
    kind_n = (kind or "industry").strip().lower()
    if kind_n not in {"industry", "concept"}:
        kind_n = "industry"
    code_n = normalize_board_code(board_code)
    if not code_n.startswith("BK"):
        raise ValueError(f"无效板块代码: {board_code}")

    df = fetch_board_members(
        code_n, kind=kind_n, retries=3, retry_delay=1.2
    )
    target = normalize_code(target_code) if target_code else ""
    members: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        mcode = str(r.get("代码") or "").strip().zfill(6)
        if not mcode.isdigit():
            continue
        pe = _to_float(r.get("市盈率-动态"))
        pb = _to_float(r.get("市净率"))
        members.append(
            {
                "code": mcode,
                "name": str(r.get("名称") or "").strip(),
                "price": _to_float(r.get("最新价")),
                "change_pct": _to_float(r.get("涨跌幅")),
                "pe": pe,
                "pb": pb,
                "total_market_cap": _to_float(r.get("总市值")),
                "turnover_rate": _to_float(r.get("换手率")),
                "is_target": bool(target and mcode == target),
            }
        )

    # 目标股置顶，其余按涨跌幅排序后截断
    members.sort(
        key=lambda x: (
            0 if x["is_target"] else 1,
            -(x["change_pct"] if x["change_pct"] is not None else -999),
        )
    )
    clipped = members[: max(1, int(limit))]
    pe_vals = [m["pe"] for m in clipped if isinstance(m.get("pe"), (int, float)) and m["pe"] > 0]
    pb_vals = [m["pb"] for m in clipped if isinstance(m.get("pb"), (int, float)) and m["pb"] > 0]
    chg_vals = [
        m["change_pct"]
        for m in clipped
        if isinstance(m.get("change_pct"), (int, float))
    ]

    def _median(vals: list[float]) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else round((s[mid - 1] + s[mid]) / 2, 2)

    target_row = next((m for m in clipped if m.get("is_target")), None)
    return {
        "board_code": code_n,
        "kind": kind_n,
        "member_total": len(members),
        "returned": len(clipped),
        "members": clipped,
        "summary": {
            "pe_median": _median(pe_vals),
            "pb_median": _median(pb_vals),
            "change_pct_median": _median(chg_vals),
            "up_count": sum(1 for c in chg_vals if c > 0),
            "down_count": sum(1 for c in chg_vals if c < 0),
            "target": target_row,
        },
        "source": "eastmoney-board-members",
        "note": "成分股估值来自东财板块行情字段，适合板块内同业联动对比。",
    }


def pick_primary_industry_board(industry_boards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """优先选更细分的行业板块（名称含 Ⅱ/Ⅲ），否则取第一个。"""
    if not industry_boards:
        return None
    ranked = sorted(
        industry_boards,
        key=lambda x: (
            0 if ("Ⅲ" in (x.get("board_name") or "") or "III" in (x.get("board_name") or "").upper()) else 1,
            0 if ("Ⅱ" in (x.get("board_name") or "") or "II" in (x.get("board_name") or "").upper()) else 1,
            # 无层级标记时优先更细分（名称更长）的行业板
            -len(x.get("board_name") or ""),
        ),
    )
    return ranked[0]


def _metric_from_items(items: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for k in keys:
        if k in items:
            v = _to_float(items.get(k))
            if v is not None:
                return v
    return None


def _trend_yoy(trends: list[dict[str, Any]], metric_keys: tuple[str, ...]) -> dict[str, Any] | None:
    for t in trends or []:
        metric = str(t.get("metric") or "")
        if any(k in metric for k in metric_keys):
            return {
                "metric": metric,
                "latest": t.get("latest"),
                "change_pct": t.get("change_pct"),
                "direction": t.get("direction"),
                "label": t.get("label"),
                "latest_period": t.get("latest_period"),
            }
    return None


def _peer_fundamentals_row(
    *,
    code: str,
    name: str,
    quote: Any,
    finance: Any,
    is_target: bool,
) -> dict[str, Any]:
    items = {}
    report_date = None
    if finance and getattr(finance, "latest_indicators", None):
        items = finance.latest_indicators.items or {}
        report_date = finance.latest_indicators.report_date
    trends = list(getattr(finance, "trends", None) or []) if finance else []

    revenue = _metric_from_items(items, ("营业总收入", "营业收入"))
    net_profit = _metric_from_items(items, ("归母净利润", "净利润"))
    op_cash = _metric_from_items(items, ("经营现金流量净额",))
    roe = _metric_from_items(items, ("净资产收益率(ROE)", "净资产收益率", "ROE"))
    rev_growth = _metric_from_items(items, ("营业总收入增长率", "营业收入增长率"))
    profit_growth = _metric_from_items(
        items, ("归属母公司净利润增长率", "净利润增长率", "归母净利润增长率")
    )

    rev_trend = _trend_yoy(trends, ("营业总收入", "营业收入"))
    profit_trend = _trend_yoy(trends, ("归母净利润", "净利润"))
    cash_trend = _trend_yoy(trends, ("经营现金流量净额",))

    return {
        "code": code,
        "name": name or (getattr(quote, "name", None) or ""),
        "is_target": is_target,
        "price": getattr(quote, "price", None),
        "change_pct": getattr(quote, "change_pct", None),
        "pe": getattr(quote, "pe", None),
        "pb": getattr(quote, "pb", None),
        "total_market_cap": getattr(quote, "total_market_cap", None),
        "report_date": report_date,
        "revenue": revenue,
        "revenue_yoy_pct": rev_growth
        if rev_growth is not None
        else (rev_trend or {}).get("change_pct"),
        "revenue_trend_label": (rev_trend or {}).get("label"),
        "net_profit": net_profit,
        "net_profit_yoy_pct": profit_growth
        if profit_growth is not None
        else (profit_trend or {}).get("change_pct"),
        "net_profit_trend_label": (profit_trend or {}).get("label"),
        "operating_cashflow": op_cash,
        "operating_cashflow_yoy_pct": (cash_trend or {}).get("change_pct"),
        "operating_cashflow_trend_label": (cash_trend or {}).get("label"),
        "roe": roe,
    }


def _rank_target(rows: list[dict[str, Any]], field: str, *, higher_better: bool) -> dict[str, Any]:
    scored = [
        (r["code"], r.get(field))
        for r in rows
        if isinstance(r.get(field), (int, float))
    ]
    if not scored:
        return {"field": field, "rank": None, "n": 0}
    scored.sort(key=lambda x: x[1], reverse=higher_better)
    target = next((r for r in rows if r.get("is_target")), None)
    if not target or target.get(field) is None:
        return {"field": field, "rank": None, "n": len(scored)}
    rank = next((i + 1 for i, (c, _) in enumerate(scored) if c == target["code"]), None)
    return {
        "field": field,
        "rank": rank,
        "n": len(scored),
        "target_value": target.get(field),
        "best_value": scored[0][1],
        "median": sorted(v for _, v in scored)[len(scored) // 2],
    }


async def analyze_board_resonance(code: str) -> dict[str, Any]:
    """个股 vs 所属行业板块的当日涨跌联动（不含概念题材）。"""
    code_n = normalize_code(code)
    membership = await asyncio.to_thread(lookup_stock_boards, code_n)
    quote = await fetch_quote(code_n)
    stock_chg = quote.change_pct
    quote_maps = await asyncio.to_thread(load_board_quote_maps, refresh=False)

    industry: list[dict[str, Any]] = []
    for b in membership.get("industry") or []:
        bc = b.get("board_code") or ""
        name = b.get("board_name") or ""
        if _is_style_or_index(name):
            continue
        q = quote_maps.get(bc) or {}
        board_chg = q.get("change_pct")
        industry.append(
            {
                "board_code": bc,
                "board_name": name,
                "kind": "industry",
                "board_change_pct": board_chg,
                "stock_change_pct": stock_chg,
                "spread_pct": (
                    round(stock_chg - board_chg, 2)
                    if stock_chg is not None and board_chg is not None
                    else None
                ),
                "align": _align_label(stock_chg, board_chg),
                "relative": _relative_label(stock_chg, board_chg),
                "up_count": q.get("up_count"),
                "down_count": q.get("down_count"),
                "leader": q.get("leader") or "",
            }
        )

    primary = pick_primary_industry_board(industry)
    focus = industry
    same = sum(1 for x in focus if x["align"] == "同向")
    diverge = sum(1 for x in focus if x["align"] == "背离")
    stronger = sum(1 for x in focus if x["relative"] == "强于板块")
    weaker = sum(1 for x in focus if x["relative"] == "弱于板块")
    rising_boards = sum(
        1 for x in focus if isinstance(x.get("board_change_pct"), (int, float)) and x["board_change_pct"] > 0
    )
    falling_boards = sum(
        1 for x in focus if isinstance(x.get("board_change_pct"), (int, float)) and x["board_change_pct"] < 0
    )

    if same + diverge == 0:
        resonance = "数据不足，暂无法判断行业板块联动"
    elif same >= max(diverge, 1) and rising_boards >= falling_boards and _direction(stock_chg) in {
        "上涨",
        "平盘",
    }:
        if _direction(stock_chg) == "平盘" and rising_boards <= falling_boards:
            resonance = "弱波动同步：个股与所属行业板块涨跌幅度有限，联动信号偏弱"
        else:
            resonance = "偏多联动：个股与多数所属行业板块同向上涨或同步偏强"
    elif same >= max(diverge, 1) and falling_boards >= rising_boards and _direction(stock_chg) in {
        "下跌",
        "平盘",
    }:
        resonance = "偏空联动：个股与多数所属行业板块同向下跌或同步偏弱"
    elif diverge > same:
        resonance = "结构背离：个股走势与部分所属行业板块不一致"
    else:
        resonance = "弱联动/分化：行业板块内部涨跌不一，个股跟随不完整"

    stock_info = membership.get("stock") or {}
    return {
        "code": code_n,
        "name": stock_info.get("name") or quote.name or "",
        "stock_change_pct": stock_chg,
        "stock_price": quote.price,
        "industry": industry,
        "primary_industry_board": primary,
        "summary": {
            "resonance": resonance,
            "focus_board_count": len(focus),
            "align_same": same,
            "align_diverge": diverge,
            "stronger_than_board": stronger,
            "weaker_than_board": weaker,
            "rising_boards": rising_boards,
            "falling_boards": falling_boards,
        },
        "source": "eastmoney-f10 + board-clist",
        "note": "仅分析行业板块联动，不含概念/题材/风格标签（避免牵强归因）。",
    }


async def compare_board_fundamentals(
    code: str,
    *,
    peer_limit: int = 6,
    board_code: str | None = None,
) -> dict[str, Any]:
    """拉取主行业板块成分股，再取行情+财务，横向对比业绩/PE/PB/现金流。"""
    code_n = normalize_code(code)
    membership = await asyncio.to_thread(lookup_stock_boards, code_n)
    industry = [
        b
        for b in (membership.get("industry") or [])
        if not _is_style_or_index(str(b.get("board_name") or ""))
    ]
    primary = None
    if board_code:
        bc = str(board_code).strip().upper()
        primary = next((b for b in industry if b.get("board_code") == bc), None)
        if not primary:
            primary = {"board_code": bc, "board_name": bc}
    if not primary:
        primary = pick_primary_industry_board(industry)
    if not primary:
        return {
            "code": code_n,
            "found": False,
            "message": "未找到可用的行业板块，无法做板块内基本面对比",
            "peers": [],
        }

    snap = await asyncio.to_thread(
        fetch_board_member_snapshot,
        primary["board_code"],
        kind="industry",
        limit=80,
        target_code=code_n,
    )
    members = list(snap.get("members") or [])
    target = next((m for m in members if m.get("is_target")), None)
    others = [m for m in members if not m.get("is_target")]

    # 优先选取市值接近标的的同业，保证横向可比
    target_mcap = (target or {}).get("total_market_cap")
    if isinstance(target_mcap, (int, float)):
        others.sort(
            key=lambda m: abs((m.get("total_market_cap") or 0) - target_mcap)
        )
    else:
        others.sort(
            key=lambda m: -(m.get("total_market_cap") or 0)
        )

    n_peers = max(1, min(int(peer_limit), 8))
    selected = ([target] if target else [{"code": code_n, "name": "", "is_target": True}]) + others[
        : max(0, n_peers - 1)
    ]

    async def _one(m: dict[str, Any]) -> dict[str, Any]:
        c = normalize_code(str(m.get("code") or ""))
        name = str(m.get("name") or "")
        try:
            q = await fetch_quote(c)
            fin = await asyncio.to_thread(fetch_finance, c, name or q.name)
            return _peer_fundamentals_row(
                code=c,
                name=name or q.name or "",
                quote=q,
                finance=fin,
                is_target=bool(m.get("is_target") or c == code_n),
            )
        except Exception as exc:
            log_caught(logger, "board peer fundamentals failed code=%s", c, exc=exc)
            return {
                "code": c,
                "name": name,
                "is_target": bool(m.get("is_target") or c == code_n),
                "error": str(exc),
            }

    rows = list(await asyncio.gather(*[_one(m) for m in selected]))
    rows.sort(key=lambda r: (0 if r.get("is_target") else 1, r.get("code") or ""))

    ranks = {
        "pe": _rank_target(rows, "pe", higher_better=False),  # PE 越低通常越便宜
        "pb": _rank_target(rows, "pb", higher_better=False),
        "net_profit_yoy_pct": _rank_target(rows, "net_profit_yoy_pct", higher_better=True),
        "revenue_yoy_pct": _rank_target(rows, "revenue_yoy_pct", higher_better=True),
        "operating_cashflow": _rank_target(rows, "operating_cashflow", higher_better=True),
        "roe": _rank_target(rows, "roe", higher_better=True),
    }

    return {
        "code": code_n,
        "found": True,
        "board_code": primary.get("board_code"),
        "board_name": primary.get("board_name"),
        "board_member_total": snap.get("member_total"),
        "compared_count": len(rows),
        "peers": rows,
        "ranks": ranks,
        "focus": ["业绩(收入/利润及同比)", "市盈率PE", "市净率PB", "经营现金流", "ROE"],
        "source": "eastmoney-board-members + quote/finance",
        "note": (
            "同业样本取自主行业板块成分股；优先选择市值接近标的的公司。"
            "请重点横向对比业绩增速、PE/PB 与经营现金流，判断板块内相对价值。"
        ),
    }
