from __future__ import annotations

from pydantic import BaseModel, Field


class StockSuggestItem(BaseModel):
    code: str = Field(..., description="股票代码")
    name: str = Field(..., description="股票名称")
    initials: str = Field("", description="拼音首字母")
    market: str = Field("", description="市场：SH/SZ/BJ")
    board: str = Field("", description="板块：主板/创业板/科创板/北交所")
    industry: str = Field("", description="所属行业")


class StockSuggestResponse(BaseModel):
    query: str
    total: int = Field(..., description="匹配总数")
    items: list[StockSuggestItem] = Field(default_factory=list)
    universe_ready: bool = True
    universe_count: int = 0
