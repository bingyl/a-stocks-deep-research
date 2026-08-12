from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class QuoteInfo(BaseModel):
    code: str = Field(..., description="股票代码")
    name: str = Field(..., description="股票名称")
    price: Optional[float] = Field(None, description="最新价")
    open: Optional[float] = Field(None, description="今开")
    high: Optional[float] = Field(None, description="最高")
    low: Optional[float] = Field(None, description="最低")
    prev_close: Optional[float] = Field(None, description="昨收")
    change: Optional[float] = Field(None, description="涨跌额")
    change_pct: Optional[float] = Field(None, description="涨跌幅(%)")
    volume: Optional[float] = Field(None, description="成交量(手)")
    amount: Optional[float] = Field(None, description="成交额(万元)")
    turnover_rate: Optional[float] = Field(None, description="换手率(%)")
    pe: Optional[float] = Field(None, description="市盈率")
    pb: Optional[float] = Field(None, description="市净率")
    total_market_cap: Optional[float] = Field(None, description="总市值(亿元)")
    float_market_cap: Optional[float] = Field(None, description="流通市值(亿元)")
    amplitude: Optional[float] = Field(None, description="振幅(%)")
    high_limit: Optional[float] = Field(None, description="涨停价")
    low_limit: Optional[float] = Field(None, description="跌停价")
    update_time: Optional[str] = Field(None, description="行情时间")
    source: str = Field("tencent", description="数据来源")


class FinanceIndicator(BaseModel):
    report_date: Optional[str] = Field(None, description="报告期")
    items: dict[str, Any] = Field(default_factory=dict, description="财务指标")


class FinanceOverview(BaseModel):
    code: str
    name: Optional[str] = None
    latest_indicators: Optional[FinanceIndicator] = None
    recent_indicators: list[FinanceIndicator] = Field(default_factory=list)
    income_summary: list[dict[str, Any]] = Field(
        default_factory=list, description="利润表摘要(近年，可含业绩预告行)"
    )
    balance_summary: list[dict[str, Any]] = Field(
        default_factory=list, description="净资产与财务风险指标摘要(近年)"
    )
    earnings_forecasts: list[dict[str, Any]] = Field(
        default_factory=list,
        description="业绩预告（区间值；含正式披露预约时间）",
    )
    trends: list[dict[str, Any]] = Field(
        default_factory=list,
        description="关键指标同比/环比趋势（含升降中文说明）",
    )
    source: str = "eastmoney/sina"


class StockOverview(BaseModel):
    code: str
    name: str
    quote: QuoteInfo
    finance: FinanceOverview
