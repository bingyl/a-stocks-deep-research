from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ResearchReportCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    name: Optional[str] = None
    question: Optional[str] = None
    model: str = ""
    analysis: str = Field(..., min_length=1)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_rounds: int = 0
    framework: str = ""


class ResearchReportSummary(BaseModel):
    id: int
    code: str
    name: str = ""
    model: str = ""
    tool_rounds: int = 0
    created_at: str
    preview: str = Field("", description="一句话结论摘要")
    message_count: int = 0
    status: str = "done"
    status_label: str = "已完成"
    status_detail_text: str = Field("", description="当前进度一句话")
    status_detail: dict[str, Any] = Field(default_factory=dict)
    analysis_run_id: str = Field("", description="当前/最近一轮分析行为 id")


class ResearchReportDetail(BaseModel):
    id: int
    code: str
    name: str = ""
    question: Optional[str] = None
    model: str = ""
    analysis: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_rounds: int = 0
    framework: str = ""
    created_at: str
    message_count: int = 0
    status: str = "done"
    status_label: str = "已完成"
    status_detail_text: str = ""
    status_detail: dict[str, Any] = Field(default_factory=dict)
    analysis_run_id: str = Field("", description="当前/最近一轮分析行为 id")


class ResearchReportListResponse(BaseModel):
    total: int
    items: list[ResearchReportSummary]


class ResearchReportSaveResponse(BaseModel):
    id: int
    code: str
    name: str = ""
    created_at: str
    status: str = "done"


class ResearchChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000, description="追问内容")


class ResearchMessageItem(BaseModel):
    id: int
    report_id: int
    role: str
    content: str
    refused: bool = False
    model: str = ""
    created_at: str


class ResearchMessageListResponse(BaseModel):
    report_id: int
    total: int
    items: list[ResearchMessageItem]
