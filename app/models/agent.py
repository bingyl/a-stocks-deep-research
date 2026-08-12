from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AgentToolCall(BaseModel):
    round: int = 1
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_preview: str = ""


class AgentAnalyzeResponse(BaseModel):
    code: str
    name: Optional[str] = None
    question: Optional[str] = None
    model: str
    analysis: str = Field(..., description="Markdown 分析正文")
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    tool_rounds: int = 0
    framework: str = Field(
        "deepagents+langgraph+multi-agent",
        description="智能体框架标识",
    )
    id: Optional[int] = Field(None, description="入库后的深研记录 ID")
    created_at: Optional[str] = Field(None, description="入库时间")


class AnalyzeRequest(BaseModel):
    code: str = Field(..., description="股票代码，如 600519")
    question: str | None = Field(
        None,
        description="可选的分析侧重点，例如：关注毛利率下滑原因与竞争格局",
    )
    report_id: int | None = Field(
        None,
        description="可选。传入已取消/失败的报告 id 时，在原任务上重跑，不新建记录",
    )
