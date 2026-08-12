from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import akshare as ak
import httpx
import pandas as pd

from app.core.logging import log_caught
from app.models.stock import (
    FinanceIndicator,
    FinanceOverview,
    QuoteInfo,
    StockOverview,
)

logger = logging.getLogger(__name__)


class StockNotFoundError(Exception):
    """股票代码无效或未找到数据。"""


class StockDataError(Exception):
    """上游数据源请求失败。"""


def normalize_code(raw: str) -> str:
    """将多种输入格式规范为 6 位 A 股代码。"""
    text = (raw or "").strip().upper()
    text = text.replace(" ", "")
    text = re.sub(r"^(SH|SZ|BJ)\.?|^S[HZ]", "", text)
    text = text.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    text = re.sub(r"\D", "", text)
    if len(text) != 6:
        raise StockNotFoundError(f"无效股票代码: {raw}")
    return text


def market_prefix(code: str) -> str:
    if code.startswith(("5", "6", "9")):
        return "sh"
    if code.startswith(("0", "1", "2", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    raise StockNotFoundError(f"无法识别市场: {code}")


def to_secid(code: str) -> str:
    prefix = market_prefix(code)
    market_map = {"sh": "1", "sz": "0", "bj": "0"}
    return f"{market_map[prefix]}.{code}"


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


async def fetch_quote(code: str) -> QuoteInfo:
    """优先腾讯实时行情，失败时回退东财。"""
    code = normalize_code(code)
    try:
        return await _fetch_quote_tencent(code)
    except StockNotFoundError:
        raise
    except Exception as exc:
        log_caught(logger, "腾讯行情失败，回退东财 code=%s", code, exc=exc)
        return await _fetch_quote_eastmoney(code)


async def _fetch_quote_tencent(code: str) -> QuoteInfo:
    symbol = f"{market_prefix(code)}{code}"
    url = f"https://qt.gtimg.cn/q={symbol}"
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        resp.encoding = "gbk"
        text = resp.text.strip()

    if '=""' in text or '="' not in text:
        raise StockNotFoundError(f"未找到股票: {code}")

    payload = text.split('="', 1)[1].rstrip('";')
    fields = payload.split("~")
    if len(fields) < 45:
        raise StockDataError("腾讯行情字段不完整")

    name = fields[1]
    if not name:
        raise StockNotFoundError(f"未找到股票: {code}")

    # 腾讯成交额单位约为万元；总市值/流通市值约为亿元
    return QuoteInfo(
        code=code,
        name=name,
        price=_to_float(fields[3]),
        prev_close=_to_float(fields[4]),
        open=_to_float(fields[5]),
        volume=_to_float(fields[6]),
        change=_to_float(fields[31]),
        change_pct=_to_float(fields[32]),
        high=_to_float(fields[33]),
        low=_to_float(fields[34]),
        amount=_to_float(fields[37]),
        turnover_rate=_to_float(fields[38]),
        pe=_to_float(fields[39]),
        high_limit=_to_float(fields[47]) if len(fields) > 47 else None,
        low_limit=_to_float(fields[48]) if len(fields) > 48 else None,
        amplitude=_to_float(fields[43]),
        float_market_cap=_to_float(fields[44]),
        total_market_cap=_to_float(fields[45]),
        pb=_to_float(fields[46]) if len(fields) > 46 else None,
        update_time=fields[30] if len(fields) > 30 else None,
        source="tencent",
    )


async def _fetch_quote_eastmoney(code: str) -> QuoteInfo:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "fltt": "2",
        "invt": "2",
        "secid": to_secid(code),
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f116,f117,f162,f167,f168,f169,f170,f171",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json().get("data") or {}

    if not data or not data.get("f58"):
        raise StockNotFoundError(f"未找到股票: {code}")

    # 东财成交额为元，统一转为万元；市值为元，统一转为亿元
    amount_yuan = _to_float(data.get("f48"))
    total_cap = _to_float(data.get("f116"))
    float_cap = _to_float(data.get("f117"))

    return QuoteInfo(
        code=code,
        name=str(data.get("f58")),
        price=_to_float(data.get("f43")),
        high=_to_float(data.get("f44")),
        low=_to_float(data.get("f45")),
        open=_to_float(data.get("f46")),
        volume=_to_float(data.get("f47")),
        amount=(amount_yuan / 10000.0) if amount_yuan is not None else None,
        prev_close=_to_float(data.get("f60")),
        total_market_cap=(total_cap / 1e8) if total_cap is not None else None,
        float_market_cap=(float_cap / 1e8) if float_cap is not None else None,
        pe=_to_float(data.get("f162")),
        pb=_to_float(data.get("f167")),
        turnover_rate=_to_float(data.get("f168")),
        change=_to_float(data.get("f169")),
        change_pct=_to_float(data.get("f170")),
        amplitude=_to_float(data.get("f171")),
        source="eastmoney",
    )


def fetch_finance(code: str, name: Optional[str] = None) -> FinanceOverview:
    """拉取新浪/东财财务摘要（较快），并整理为指标与报表摘要；附带业绩预告。"""
    code = normalize_code(code)
    indicators, income, balance = _fetch_finance_from_abstract(code)
    forecasts = _fetch_earnings_forecasts(code)

    if not indicators and not income and not balance and not forecasts:
        raise StockDataError(f"暂无财务数据: {code}")

    if forecasts:
        income = _merge_forecasts_into_income(income, forecasts)

    latest = indicators[0] if indicators else None
    trends = _build_finance_trends(indicators)
    return FinanceOverview(
        code=code,
        name=name,
        latest_indicators=latest,
        recent_indicators=indicators[:6],
        income_summary=income,
        balance_summary=balance,
        earnings_forecasts=forecasts,
        trends=trends,
        source="eastmoney/sina",
    )


def _date_only(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text in {"None", "nan", "NaN"}:
        return ""
    return text[:10]


def _forecast_metric_key(raw_name: str) -> Optional[str]:
    name = str(raw_name or "").strip()
    mapping = {
        "营业收入": "营业总收入",
        "营业总收入": "营业总收入",
        "归属于上市公司股东的净利润": "归母净利润",
        "归母净利润": "归母净利润",
        "扣除非经常性损益后的净利润": "扣非净利润",
        "扣非净利润": "扣非净利润",
        "净利润": "净利润",
        "基本每股收益": "基本每股收益",
    }
    return mapping.get(name)


def _forecast_period_label(report_date: str) -> str:
    md = report_date[5:] if len(report_date) >= 10 else ""
    year = report_date[:4] if len(report_date) >= 4 else ""
    kind = {
        "03-31": "一季报（累计）",
        "06-30": "半年报/中报（累计）",
        "09-30": "三季报（累计）",
        "12-31": "年报（累计）",
    }.get(md, "财报（累计）")
    return f"{year}年{kind}" if year else kind


def _fetch_em_datacenter(
    report_name: str,
    *,
    filter_str: str,
    sort_columns: str = "",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "source": "WEB",
        "client": "WEB",
    }
    if sort_columns:
        params["sortColumns"] = sort_columns
        params["sortTypes"] = "-1"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Referer": "https://data.eastmoney.com/",
    }
    with httpx.Client(timeout=25.0, follow_redirects=True) as client:
        resp = client.get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        payload = resp.json()
    return list(((payload.get("result") or {}).get("data")) or [])


def _fetch_earnings_forecasts(code: str) -> list[dict[str, Any]]:
    """东财个股业绩预告 + 正式报表预约披露时间。"""
    code = normalize_code(code)
    try:
        predict_rows = _fetch_em_datacenter(
            "RPT_PUBLIC_OP_NEWPREDICT",
            filter_str=f'(SECURITY_CODE="{code}")',
            sort_columns="NOTICE_DATE",
            page_size=40,
        )
    except Exception as exc:
        log_caught(logger, "earnings forecast failed code=%s", code, exc=exc)
        return []

    if not predict_rows:
        return []

    appoint_by_date: dict[str, dict[str, Any]] = {}
    try:
        appoint_rows = _fetch_em_datacenter(
            "RPT_PUBLIC_BS_APPOIN",
            filter_str=f'(SECURITY_CODE="{code}")',
            sort_columns="FIRST_APPOINT_DATE",
            page_size=30,
        )
        for item in appoint_rows:
            rd = _date_only(item.get("REPORT_DATE"))
            if rd:
                appoint_by_date[rd] = item
    except Exception as exc:
        log_caught(logger, "appoint disclosure failed code=%s", code, exc=exc)

    # 按报告期聚合多指标；同一报告期保留最新公告日
    by_period: dict[str, dict[str, Any]] = {}
    for item in predict_rows:
        report_date = _date_only(item.get("REPORT_DATE"))
        notice_date = _date_only(item.get("NOTICE_DATE"))
        metric_key = _forecast_metric_key(str(item.get("PREDICT_FINANCE") or ""))
        if not report_date or not metric_key:
            continue

        bucket = by_period.get(report_date)
        if bucket is None or notice_date > str(bucket.get("公告日期") or ""):
            appoint = appoint_by_date.get(report_date) or {}
            actual = _date_only(appoint.get("ACTUAL_PUBLISH_DATE"))
            appoint_date = _date_only(
                appoint.get("APPOINT_PUBLISH_DATE")
                or appoint.get("FIRST_APPOINT_DATE")
            )
            published = str(appoint.get("IS_PUBLISH") or "") == "1" or bool(actual)
            bucket = {
                "报告期": report_date,
                "报告类型": _forecast_period_label(report_date),
                "报告口径": "累计",
                "is_forecast": True,
                "预告": True,
                "公告日期": notice_date,
                "正式披露日期": actual or appoint_date,
                "正式披露预约日": appoint_date,
                "正式已披露": published,
                "正式披露状态": "已披露" if published else ("已预约" if appoint_date else "待定"),
                "报告类型名称": str(appoint.get("REPORT_TYPE_NAME") or "").strip(),
                "预告类型": "",
                "业绩变动原因": str(item.get("CHANGE_REASON_EXPLAIN") or "").strip(),
            }
            by_period[report_date] = bucket
        elif notice_date < str(bucket.get("公告日期") or ""):
            # 更旧公告，跳过整组（已有更新）
            continue

        # 同一公告日内写入指标
        if notice_date != str(bucket.get("公告日期") or ""):
            continue

        lower = _to_float(item.get("PREDICT_AMT_LOWER"))
        upper = _to_float(item.get("PREDICT_AMT_UPPER"))
        mid = _to_float(item.get("FORECAST_JZ"))
        if mid is None and lower is not None and upper is not None:
            mid = (lower + upper) / 2.0
        elif mid is None:
            mid = lower if lower is not None else upper

        yoy = _to_float(item.get("INCREASE_JZ"))
        if yoy is None:
            lo = _to_float(item.get("ADD_AMP_LOWER"))
            hi = _to_float(item.get("ADD_AMP_UPPER"))
            if lo is not None and hi is not None:
                yoy = (lo + hi) / 2.0
            elif lo is not None:
                yoy = lo
            elif hi is not None:
                yoy = hi

        bucket[metric_key] = mid
        if lower is not None:
            bucket[f"{metric_key}_下限"] = lower
        if upper is not None:
            bucket[f"{metric_key}_上限"] = upper
        if yoy is not None:
            bucket[f"{metric_key}_同比"] = yoy

        predict_type = str(item.get("PREDICT_TYPE") or "").strip()
        # 归母净利润的预告类型更有代表性
        if metric_key == "归母净利润" and predict_type:
            bucket["预告类型"] = predict_type
        elif predict_type and not bucket.get("预告类型"):
            bucket["预告类型"] = predict_type

        content = str(item.get("PREDICT_CONTENT") or "").strip()
        if content:
            contents = bucket.setdefault("预告说明", [])
            if isinstance(contents, list) and content not in contents:
                contents.append(content)

    rows = list(by_period.values())
    for row in rows:
        contents = row.pop("预告说明", None)
        if isinstance(contents, list) and contents:
            row["预告说明"] = "；".join(contents[:3])

    rows.sort(key=lambda r: str(r.get("报告期") or ""), reverse=True)
    # 正式已披露的预告不再展示（利润表应已有正式数）
    return [r for r in rows if not r.get("正式已披露")][:6]


def _merge_forecasts_into_income(
    income: list[dict[str, Any]],
    forecasts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing_cum = {
        str(r.get("报告期") or "")
        for r in income
        if str(r.get("报告口径") or "累计") == "累计" and not r.get("is_forecast")
    }
    merged = list(income)
    for fc in forecasts:
        date = str(fc.get("报告期") or "")
        if not date or date in existing_cum:
            continue
        merged.append(fc)

    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in merged:
        by_date.setdefault(str(row.get("报告期") or ""), []).append(row)

    ordered: list[dict[str, Any]] = []
    for date in sorted(by_date.keys(), reverse=True):
        rows = by_date[date]

        def _rank(r: dict[str, Any]) -> tuple[int, int]:
            # 预告优先于正式累计（同日一般不会并存）；累计优先于单季
            forecast_rank = 0 if r.get("is_forecast") else 1
            scope_rank = 0 if str(r.get("报告口径") or "累计") == "累计" else 1
            return (forecast_rank, scope_rank)

        rows.sort(key=_rank)
        ordered.extend(rows)
    return ordered[:28]


def _fmt_trend_value(metric: str, value: float) -> str:
    abs_v = abs(value)
    if any(k in metric for k in ("收入", "利润", "成本", "现金流", "净资产", "商誉")):
        if abs_v >= 1e8:
            return f"{value / 1e8:.2f}亿"
        if abs_v >= 1e4:
            return f"{value / 1e4:.2f}万"
    if any(k in metric for k in ("率", "ROE", "ROA", "增长")):
        return f"{value:.2f}%"
    return f"{value:.2f}"


def _build_finance_trends(indicators: list[FinanceIndicator]) -> list[dict[str, Any]]:
    """对比最新报告期 vs 去年同期（优先）或上一报告期，给出升降说明。"""
    if len(indicators) < 2:
        return []

    latest = indicators[0]
    latest_date = latest.report_date or ""
    compare: FinanceIndicator | None = None
    compare_kind = "同比"

    # 优先找去年同月日（如 2024-12-31 vs 2023-12-31）
    if len(latest_date) >= 10:
        yoy = f"{int(latest_date[:4]) - 1}{latest_date[4:]}"
        for ind in indicators[1:]:
            if (ind.report_date or "") == yoy:
                compare = ind
                compare_kind = "同比"
                break

    # 其次：上一年度年报（两端都是 12-31）
    if compare is None and latest_date.endswith("12-31"):
        for ind in indicators[1:]:
            if (ind.report_date or "").endswith("12-31"):
                compare = ind
                compare_kind = "同比(年报)"
                break

    # 再次：相邻报告期（环比）
    if compare is None:
        compare = indicators[1]
        compare_kind = "环比(上一报告期)"

    focus_metrics = [
        "营业总收入",
        "归母净利润",
        "净利润",
        "扣非净利润",
        "基本每股收益",
        "毛利率",
        "销售净利率",
        "净资产收益率(ROE)",
        "资产负债率",
        "经营现金流量净额",
        "营业总收入增长率",
        "归属母公司净利润增长率",
    ]

    trends: list[dict[str, Any]] = []
    for metric in focus_metrics:
        cur = latest.items.get(metric)
        prev = compare.items.get(metric) if compare else None
        cur_f = _to_float(cur)
        prev_f = _to_float(prev)
        if cur_f is None or prev_f is None:
            # 增长率字段本身已是趋势，单独输出
            if cur_f is not None and "增长率" in metric:
                direction = "上升" if cur_f > 0 else ("下降" if cur_f < 0 else "持平")
                trends.append(
                    {
                        "metric": metric,
                        "latest_period": latest.report_date,
                        "compare_period": None,
                        "compare_kind": "指标自带增速",
                        "latest": cur_f,
                        "latest_fmt": _fmt_trend_value(metric, cur_f),
                        "prior": None,
                        "prior_fmt": None,
                        "change": None,
                        "change_pct": cur_f,
                        "direction": direction,
                        "label": f"{metric}为 {cur_f:.2f}%（{direction}）",
                    }
                )
            continue

        change = cur_f - prev_f
        if abs(prev_f) < 1e-12:
            change_pct = None
            if change > 0:
                direction = "上升"
                label = f"{metric}：{_fmt_trend_value(metric, cur_f)}，相对对照期由近零转正（{compare_kind}）"
            elif change < 0:
                direction = "下降"
                label = f"{metric}：{_fmt_trend_value(metric, cur_f)}，相对对照期由近零转负（{compare_kind}）"
            else:
                direction = "持平"
                label = f"{metric}：{_fmt_trend_value(metric, cur_f)}，与对照期持平（{compare_kind}）"
        else:
            change_pct = (change / abs(prev_f)) * 100
            # 比率类看百分点变化更直观
            if any(k in metric for k in ("率", "ROE", "ROA")) and "增长" not in metric:
                direction = "上升" if change > 0.05 else ("下降" if change < -0.05 else "持平")
                arrow = "↑" if direction == "上升" else ("↓" if direction == "下降" else "→")
                label = (
                    f"{metric}：{_fmt_trend_value(metric, cur_f)} "
                    f"（{compare_kind} {arrow} {abs(change):.2f} 个百分点，"
                    f"对照期 {_fmt_trend_value(metric, prev_f)}）"
                )
            else:
                direction = "上升" if change_pct > 1 else ("下降" if change_pct < -1 else "持平")
                arrow = "↑" if direction == "上升" else ("↓" if direction == "下降" else "→")
                verb = "增长" if direction == "上升" else ("下降" if direction == "下降" else "基本持平")
                label = (
                    f"{metric}：{_fmt_trend_value(metric, cur_f)} "
                    f"（{compare_kind}{verb} {abs(change_pct):.1f}% {arrow}，"
                    f"对照期 {_fmt_trend_value(metric, prev_f)}）"
                )

        trends.append(
            {
                "metric": metric,
                "latest_period": latest.report_date,
                "compare_period": compare.report_date if compare else None,
                "compare_kind": compare_kind,
                "latest": cur_f,
                "latest_fmt": _fmt_trend_value(metric, cur_f),
                "prior": prev_f,
                "prior_fmt": _fmt_trend_value(metric, prev_f),
                "change": change,
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
                "direction": direction,
                "label": label,
            }
        )

    return trends


def _fetch_finance_from_abstract(
    code: str,
) -> tuple[list[FinanceIndicator], list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        df = ak.stock_financial_abstract(symbol=code)
    except Exception as exc:
        log_caught(logger, "akshare financial_abstract 失败 code=%s", code, exc=exc)
        return [], [], []

    if df is None or df.empty:
        return [], [], []

    period_cols = [c for c in df.columns if str(c).isdigit() and len(str(c)) == 8]
    period_cols = sorted(period_cols, reverse=True)
    if not period_cols:
        return [], [], []

    # 优先使用「常用指标」分组，避免同名指标被营运能力等分组覆盖
    preferred_section = "常用指标"
    metric_map: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        metric = str(row.get("指标", "")).strip()
        section = str(row.get("选项", "")).strip()
        if not metric or metric in {"nan", "None"}:
            continue
        if metric in metric_map and section != preferred_section:
            continue
        if section == preferred_section or metric not in metric_map:
            metric_map[metric] = {p: row.get(p) for p in period_cols}

    # 补充财务风险 / 营运能力中的常用比率
    extra_metrics = {
        "流动比率",
        "速动比率",
        "权益乘数",
        "产权比率",
        "现金比率",
        "应收账款周转率",
        "存货周转率",
        "总资产周转率",
        "营业利润率",
        "营业总收入增长率",
        "归属母公司净利润增长率",
    }
    for _, row in df.iterrows():
        metric = str(row.get("指标", "")).strip()
        if metric in extra_metrics and metric not in metric_map:
            metric_map[metric] = {p: row.get(p) for p in period_cols}

    def value_of(metric: str, period: str) -> Any:
        raw = metric_map.get(metric, {}).get(period)
        if raw is None or (isinstance(raw, float) and pd.isna(raw)) or pd.isna(raw):
            return None
        parsed = _to_float(raw)
        return parsed if parsed is not None else raw

    def pick_exact(period: str, keys: list[str]) -> dict[str, Any]:
        items: dict[str, Any] = {}
        for key in keys:
            val = value_of(key, period)
            if val is not None:
                items[key] = val
        return items

    indicator_keys = [
        "归母净利润",
        "营业总收入",
        "营业成本",
        "净利润",
        "扣非净利润",
        "基本每股收益",
        "每股净资产",
        "每股现金流",
        "经营现金流量净额",
        "净资产收益率(ROE)",
        "总资产报酬率(ROA)",
        "毛利率",
        "销售净利率",
        "营业利润率",
        "资产负债率",
        "流动比率",
        "速动比率",
        "总资产周转率",
        "存货周转率",
        "应收账款周转率",
        "营业总收入增长率",
        "归属母公司净利润增长率",
    ]

    indicators: list[FinanceIndicator] = []
    for period in period_cols[:16]:
        items = pick_exact(period, indicator_keys)
        if items:
            indicators.append(
                FinanceIndicator(
                    report_date=f"{period[:4]}-{period[4:6]}-{period[6:]}",
                    items=items,
                )
            )

    # 利润表金额类字段可做单季差分；比率/增速不差分
    income_amount_keys = [
        "营业总收入",
        "营业成本",
        "归母净利润",
        "净利润",
        "扣非净利润",
        "经营现金流量净额",
    ]
    income_keys = income_amount_keys + [
        "基本每股收益",
        "营业总收入增长率",
        "归属母公司净利润增长率",
    ]
    balance_keys = [
        "股东权益合计(净资产)",
        "商誉",
        "资产负债率",
        "流动比率",
        "速动比率",
        "权益乘数",
        "产权比率",
        "现金比率",
    ]

    def period_label(period: str, *, scope: str) -> str:
        year = period[:4]
        md = period[4:]
        if scope == "单季":
            qmap = {
                "0331": "第一季度（单季）",
                "0630": "第二季度（单季）",
                "0930": "第三季度（单季）",
                "1231": "第四季度（单季）",
            }
            return f"{year}年{qmap.get(md, '单季')}"
        cmap = {
            "0331": "一季报（累计）",
            "0630": "半年报/中报（含一二季度累计）",
            "0930": "三季报（累计）",
            "1231": "年报（累计）",
        }
        return f"{year}年{cmap.get(md, '财报（累计）')}"

    income: list[dict[str, Any]] = []
    balance: list[dict[str, Any]] = []
    # 取足够多报告期，覆盖近 3～4 年四季报
    use_periods = period_cols[:16]
    cum_by_period: dict[str, dict[str, Any]] = {}

    for period in use_periods:
        report = f"{period[:4]}-{period[4:6]}-{period[6:]}"
        income_items = pick_exact(period, income_keys)
        balance_items = pick_exact(period, balance_keys)
        if income_items:
            row = {
                "报告期": report,
                "报告类型": period_label(period, scope="累计"),
                "报告口径": "累计",
                **income_items,
            }
            income.append(row)
            cum_by_period[period] = income_items
        if balance_items:
            balance.append(
                {
                    "报告期": report,
                    "报告类型": period_label(period, scope="累计"),
                    "报告口径": "累计",
                    **balance_items,
                }
            )

    # 由累计报表推导单季度：Q2=中报-一季报，Q3=三季报-中报，Q4=年报-三季报
    prev_cum_md = {"0630": "0331", "0930": "0630", "1231": "0930"}
    single_rows: list[dict[str, Any]] = []
    for period in use_periods:
        md = period[4:]
        year = period[:4]
        report = f"{year}-{period[4:6]}-{period[6:]}"
        cur = cum_by_period.get(period)
        if not cur:
            continue
        if md == "0331":
            # 一季报累计 = 一季度单季
            single = {k: cur.get(k) for k in income_amount_keys if cur.get(k) is not None}
            if cur.get("基本每股收益") is not None:
                single["基本每股收益"] = cur.get("基本每股收益")
        else:
            prev_md = prev_cum_md.get(md)
            if not prev_md:
                continue
            prev = cum_by_period.get(f"{year}{prev_md}")
            if not prev:
                continue
            single = {}
            for key in income_amount_keys:
                a = _to_float(cur.get(key))
                b = _to_float(prev.get(key))
                if a is None or b is None:
                    continue
                single[key] = a - b
        if not single:
            continue
        single_rows.append(
            {
                "报告期": report,
                "报告类型": period_label(period, scope="单季"),
                "报告口径": "单季",
                **single,
            }
        )

    # 合并展示：同一报告期末，先累计后单季；整体按日期新→旧
    merged: list[dict[str, Any]] = []
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in income + single_rows:
        by_date.setdefault(str(row["报告期"]), []).append(row)
    for date in sorted(by_date.keys(), reverse=True):
        rows = by_date[date]
        rows.sort(key=lambda r: 0 if r.get("报告口径") == "累计" else 1)
        merged.extend(rows)

    income = merged[:24]
    balance = balance[:16]

    return indicators, income, balance


async def fetch_overview(code: str):
    quote = await fetch_quote(code)
    finance = await asyncio.to_thread(fetch_finance, quote.code, quote.name)
    return StockOverview(
        code=quote.code,
        name=quote.name,
        quote=quote,
        finance=finance,
    )
