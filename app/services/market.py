from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from sqlalchemy import select

from app.core.logging import log_caught
from app.persistence.db.models import Stock
from app.services.stock import (
    StockDataError,
    fetch_quote,
    market_prefix,
    normalize_code,
    to_secid,
)

logger = logging.getLogger(__name__)

KLT_MAP = {
    "daily": "101",
    "weekly": "102",
    "monthly": "103",
}


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", "--", "None", "nan", "NaN"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


async def fetch_kline(
    code: str,
    *,
    period: str = "daily",
    limit: int = 120,
    adjust: str = "qfq",
) -> dict[str, Any]:
    """历史 K 线：优先东财，失败回退腾讯/新浪。"""
    code = normalize_code(code)
    limit = max(20, min(int(limit), 250))
    logger.info("fetch_kline code=%s period=%s limit=%s", code, period, limit)

    errors: list[str] = []
    for fetcher in (_fetch_kline_eastmoney, _fetch_kline_tencent, _fetch_kline_sina):
        try:
            return await fetcher(code, period=period, limit=limit, adjust=adjust)
        except Exception as exc:
            # 多源回退属预期路径，只记一行，避免刷整段 traceback
            errors.append(f"{fetcher.__name__}: {exc}")
            logger.warning(
                "kline fallback after %s failed: %s: %s",
                fetcher.__name__,
                type(exc).__name__,
                exc,
            )
    raise StockDataError("暂无K线数据: " + " | ".join(errors))


async def _fetch_kline_eastmoney(
    code: str,
    *,
    period: str,
    limit: int,
    adjust: str,
) -> dict[str, Any]:
    klt = KLT_MAP.get(period, "101")
    fqt = "1" if adjust == "qfq" else "0"
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": to_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": klt,
        "fqt": fqt,
        "end": "20500101",
        "lmt": str(limit),
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        payload = resp.json()

    data = payload.get("data") or {}
    raw_klines = data.get("klines") or []
    if not raw_klines:
        raise StockDataError(f"东财K线为空: {code}")

    bars: list[dict[str, Any]] = []
    for row in raw_klines:
        parts = str(row).split(",")
        if len(parts) < 6:
            continue
        bars.append(
            {
                "date": parts[0],
                "open": _to_float(parts[1]),
                "close": _to_float(parts[2]),
                "high": _to_float(parts[3]),
                "low": _to_float(parts[4]),
                "volume": _to_float(parts[5]),
                "amount": _to_float(parts[6]) if len(parts) > 6 else None,
                "amplitude": _to_float(parts[7]) if len(parts) > 7 else None,
                "change_pct": _to_float(parts[8]) if len(parts) > 8 else None,
                "change": _to_float(parts[9]) if len(parts) > 9 else None,
                "turnover": _to_float(parts[10]) if len(parts) > 10 else None,
            }
        )
    return {
        "code": code,
        "name": str(data.get("name") or ""),
        "period": period,
        "adjust": adjust,
        "count": len(bars),
        "bars": bars,
        "source": "eastmoney",
    }


async def _fetch_kline_tencent(
    code: str,
    *,
    period: str,
    limit: int,
    adjust: str,
) -> dict[str, Any]:
    if period != "daily":
        raise StockDataError("腾讯回退源仅支持日K")
    symbol = f"{market_prefix(code)}{code}"
    # param: symbol,day,,,N,qfq
    adj = "qfq" if adjust == "qfq" else ""
    param = f"{symbol},day,,,{limit},{adj}" if adj else f"{symbol},day,,,{limit}"
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        resp = await client.get(url, params={"param": param})
        resp.raise_for_status()
        payload = resp.json()

    node = ((payload.get("data") or {}).get(symbol) or {})
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise StockDataError(f"腾讯K线为空: {code}")
    bars = []
    for row in rows:
        # date, open, close, high, low, volume
        if len(row) < 6:
            continue
        bars.append(
            {
                "date": row[0],
                "open": _to_float(row[1]),
                "close": _to_float(row[2]),
                "high": _to_float(row[3]),
                "low": _to_float(row[4]),
                "volume": _to_float(row[5]),
            }
        )
    return {
        "code": code,
        "name": "",
        "period": period,
        "adjust": adjust,
        "count": len(bars),
        "bars": bars,
        "source": "tencent",
    }


async def _fetch_kline_sina(
    code: str,
    *,
    period: str,
    limit: int,
    adjust: str,
) -> dict[str, Any]:
    if period != "daily":
        raise StockDataError("新浪回退源仅支持日K")
    symbol = f"{market_prefix(code)}{code}"
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(limit)}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    }
    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        rows = resp.json()
    if not isinstance(rows, list) or not rows:
        raise StockDataError(f"新浪K线为空: {code}")
    bars = [
        {
            "date": r.get("day"),
            "open": _to_float(r.get("open")),
            "close": _to_float(r.get("close")),
            "high": _to_float(r.get("high")),
            "low": _to_float(r.get("low")),
            "volume": _to_float(r.get("volume")),
        }
        for r in rows
    ]
    return {
        "code": code,
        "name": "",
        "period": period,
        "adjust": adjust,
        "count": len(bars),
        "bars": bars,
        "source": "sina",
    }


def _sma(values: list[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    chunk = values[-window:]
    return sum(chunk) / window


def summarize_technicals(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(b["close"]) for b in bars if b.get("close") is not None]
    if len(closes) < 5:
        return {"error": "K线不足，无法计算技术指标"}

    last = closes[-1]
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    def ret(n: int) -> Optional[float]:
        if len(closes) <= n:
            return None
        base = closes[-n - 1]
        if not base:
            return None
        return round((last / base - 1) * 100, 2)

    # 简易波动：近20日涨跌幅标准差近似
    recent = closes[-20:] if len(closes) >= 20 else closes
    rets = []
    for i in range(1, len(recent)):
        if recent[i - 1]:
            rets.append((recent[i] / recent[i - 1] - 1) * 100)
    vol = None
    if rets:
        mean = sum(rets) / len(rets)
        vol = round((sum((x - mean) ** 2 for x in rets) / len(rets)) ** 0.5, 2)

    trend = "震荡"
    if ma20 and last > ma20 and (ma5 or 0) >= (ma10 or 0):
        trend = "偏多"
    elif ma20 and last < ma20 and (ma5 or 0) <= (ma10 or 0):
        trend = "偏空"

    return {
        "last_close": last,
        "ma5": round(ma5, 2) if ma5 is not None else None,
        "ma10": round(ma10, 2) if ma10 is not None else None,
        "ma20": round(ma20, 2) if ma20 is not None else None,
        "ma60": round(ma60, 2) if ma60 is not None else None,
        "return_5d_pct": ret(5),
        "return_20d_pct": ret(20),
        "return_60d_pct": ret(60),
        "volatility_20d_pct": vol,
        "trend_bias": trend,
        "latest_bar": bars[-1] if bars else None,
    }


async def fetch_technical_snapshot(code: str, limit: int = 120) -> dict[str, Any]:
    kline = await fetch_kline(code, period="daily", limit=limit)
    tech = summarize_technicals(kline["bars"])
    recent = kline["bars"][-30:]
    return {
        "code": kline["code"],
        "name": kline["name"],
        "technicals": tech,
        "recent_bars": recent,
        "source": kline["source"],
    }


async def find_industry_peers(code: str, limit: int = 12) -> dict[str, Any]:
    from app.persistence.db import init_db, async_session_scope

    code = normalize_code(code)
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(Stock, code)
        if not row:
            return {"code": code, "found": False, "peers": []}
        industry = (row.industry or "").strip()
        peers = []
        if industry:
            peer_rows = (
                await session.scalars(
                    select(Stock)
                    .where(
                        Stock.status == "listed",
                        Stock.industry == industry,
                        Stock.code != code,
                    )
                    .order_by(Stock.code)
                    .limit(limit)
                )
            ).all()
            peers = [
                {
                    "code": p.code,
                    "name": p.name,
                    "industry": p.industry,
                    "board": p.board,
                    "market": p.market,
                }
                for p in peer_rows
            ]
        return {
            "code": code,
            "name": row.name,
            "industry": industry,
            "board": row.board,
            "market": row.market,
            "peer_count": len(peers),
            "peers": peers,
        }


async def fetch_peer_valuation(code: str, limit: int = 8) -> dict[str, Any]:
    """同业综合估值对比：PE/PB/市值等 + 分位摘要（不唯 PE/PB）。"""
    code = normalize_code(code)
    meta = await find_industry_peers(code, limit=limit)
    industry = (meta.get("industry") or "").strip()
    if not industry:
        try:
            resolved = await resolve_industry_name(code)
            industry = (resolved.get("industry") or "").strip()
            if industry:
                meta = await find_industry_peers(code, limit=limit)
                industry = (meta.get("industry") or industry).strip()
        except Exception as exc:
            log_caught(logger, "peer valuation resolve industry failed", exc=exc)

    if not industry:
        # 仍无行业时，至少返回标的自身行情，便于综合估值
        try:
            q = await fetch_quote(code)
            self_row = {
                "code": q.code,
                "name": q.name,
                "price": q.price,
                "change_pct": q.change_pct,
                "pe": q.pe,
                "pb": q.pb,
                "total_market_cap": q.total_market_cap,
                "is_target": True,
            }
        except Exception as exc:
            log_caught(logger, "peer valuation 取标的行情失败 code=%s", code, exc=exc)
            self_row = {"code": code, "error": str(exc), "is_target": True}
        return {
            "code": code,
            "industry": "",
            "found": False,
            "message": "本地库暂无行业信息，仅返回标的自身估值字段；请结合财务增速/ROE 做综合判断，勿编造同业精确数字",
            "compared": [self_row],
            "summary": _peer_valuation_summary([self_row], code),
            "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    targets = [{"code": meta["code"], "name": meta["name"]}] + meta.get("peers", [])
    rows: list[dict[str, Any]] = []
    for item in targets[: limit + 1]:
        c = item["code"]
        try:
            q = await fetch_quote(c)
            rows.append(
                {
                    "code": q.code,
                    "name": q.name,
                    "price": q.price,
                    "change_pct": q.change_pct,
                    "pe": q.pe,
                    "pb": q.pb,
                    "total_market_cap": q.total_market_cap,
                    "is_target": c == code,
                }
            )
        except Exception as exc:
            log_caught(logger, "peer quote failed code=%s", c, exc=exc)
            rows.append(
                {
                    "code": c,
                    "name": item.get("name"),
                    "error": str(exc),
                    "is_target": c == code,
                }
            )

    return {
        "code": meta.get("code") or code,
        "industry": industry,
        "found": True,
        "peer_count": meta.get("peer_count", 0),
        "compared": rows,
        "summary": _peer_valuation_summary(rows, code),
        "guidance": (
            "请综合 PE/PB/市值分位与财务增速、ROE/净利率、杠杆做判断；"
            "不要仅因单一 PE 或 PB 高低下结论。"
        ),
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _percentile(sorted_vals: list[float], value: float) -> Optional[float]:
    if not sorted_vals:
        return None
    # 简单分位：小于等于 value 的占比
    n = len(sorted_vals)
    below = sum(1 for x in sorted_vals if x <= value)
    return round(below / n * 100, 1)


def _peer_valuation_summary(rows: list[dict[str, Any]], target_code: str) -> dict[str, Any]:
    target = next((r for r in rows if r.get("is_target") or r.get("code") == target_code), None)
    peers = [r for r in rows if not (r.get("is_target") or r.get("code") == target_code)]

    def collect(key: str) -> list[float]:
        vals: list[float] = []
        for r in peers:
            v = r.get(key)
            if isinstance(v, (int, float)) and v == v and v > 0:
                vals.append(float(v))
        return sorted(vals)

    pe_list = collect("pe")
    pb_list = collect("pb")
    cap_list = collect("total_market_cap")

    def median(vals: list[float]) -> Optional[float]:
        if not vals:
            return None
        m = len(vals) // 2
        if len(vals) % 2:
            return vals[m]
        return (vals[m - 1] + vals[m]) / 2

    t_pe = target.get("pe") if target else None
    t_pb = target.get("pb") if target else None
    t_cap = target.get("total_market_cap") if target else None

    return {
        "peer_sample_size": len(peers),
        "peer_pe_median": round(median(pe_list), 2) if median(pe_list) is not None else None,
        "peer_pb_median": round(median(pb_list), 2) if median(pb_list) is not None else None,
        "peer_cap_median": round(median(cap_list), 2) if median(cap_list) is not None else None,
        "target_pe": t_pe,
        "target_pb": t_pb,
        "target_cap": t_cap,
        "target_pe_percentile": _percentile(pe_list, float(t_pe))
        if isinstance(t_pe, (int, float)) and pe_list
        else None,
        "target_pb_percentile": _percentile(pb_list, float(t_pb))
        if isinstance(t_pb, (int, float)) and pb_list
        else None,
        "target_cap_percentile": _percentile(cap_list, float(t_cap))
        if isinstance(t_cap, (int, float)) and cap_list
        else None,
        "note": "分位越高表示相对同业样本数值越高；需结合增速/ROE 等综合解读",
    }


async def resolve_industry_name(code: str) -> dict[str, Any]:
    """解析行业名：本地库优先，东财行情字段回退，名称关键词兜底。"""
    code = normalize_code(code)
    meta = await find_industry_peers(code, limit=1)
    industry = (meta.get("industry") or "").strip()
    name = str(meta.get("name") or "")
    source = "local_db" if industry else ""

    if not industry:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                ),
                "Referer": "https://quote.eastmoney.com/",
            }
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(
                    "https://push2.eastmoney.com/api/qt/stock/get",
                    params={
                        "secid": to_secid(code),
                        "fields": "f57,f58,f127",
                        "invt": "2",
                        "fltt": "2",
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                payload = resp.json().get("data") or {}
            industry = str(payload.get("f127") or "").strip()
            if not name:
                name = str(payload.get("f58") or "")
            if industry:
                source = "eastmoney"
        except Exception as exc:
            log_caught(logger, "resolve industry em failed code=%s", code, exc=exc)

    if not industry:
        for hint in (
            "银行",
            "证券",
            "保险",
            "白酒",
            "煤炭",
            "钢铁",
            "光伏",
            "半导体",
            "医药",
            "房地产",
            "航空",
            "港口",
            "电力",
            "石油",
            "汽车",
        ):
            if hint and hint in name:
                industry = hint
                source = "name_hint"
                break

    if industry:
        try:
            from app.persistence.db import init_db, async_session_scope

            await init_db()
            async with async_session_scope() as session:
                row = await session.get(Stock, code)
                if row is not None and not (row.industry or "").strip():
                    row.industry = industry
                    row.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            log_caught(logger, "persist industry failed code=%s", code, exc=exc)

    return {
        "code": code,
        "name": name,
        "industry": industry,
        "source": source,
    }
