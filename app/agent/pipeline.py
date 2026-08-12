from __future__ import annotations

import logging
import re
from typing import Annotated, Any, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from app.agent.messages import message_text
from app.agent.middleware import MONITOR_MIDDLEWARE
from app.agent.prompts import (
    STAGE1_PROMPT,
    STAGE2_PROMPT,
    STAGE3_PROMPT,
    STAGE4_PROMPT,
    STAGE5_PROMPT,
    STAGE6_PROMPT,
    build_report_user_message,
    build_stage_user_message,
)
from app.agent.stages import PIPELINE_SUBAGENT_NAME
from app.agent.tools import (
    compare_board_fundamentals,
    get_board_resonance,
    get_stock_finance,
    get_stock_overview,
    get_stock_profile,
    get_stock_quote,
    get_technical_analysis,
    search_company_news,
    search_macro_international,
    search_policy_impact,
    web_search,
)

logger = logging.getLogger(__name__)


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    code: NotRequired[str]
    name: NotRequired[str]
    question: NotRequired[str]


def _last_ai_text(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            text = message_text(msg)
            if text:
                return text
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            text = message_text(msg)
            if text:
                return text
    return ""


def parse_brief(text: str) -> tuple[str, str, str]:
    """从 task description / 用户消息解析 code、name、question。"""
    code = ""
    m = re.search(r"股票代码[：:]\s*(\d{6})", text)
    if m:
        code = m.group(1)
    if not code:
        m = re.search(r"（(\d{6})）", text)
        if m:
            code = m.group(1)
    if not code:
        m = re.search(r"\b(\d{6})\b", text)
        if m:
            code = m.group(1)

    name = ""
    m = re.search(r"分析标的[：:]\s*(.+?)（\d{6}）", text)
    if m:
        name = m.group(1).strip()

    question = ""
    m = re.search(r"用户关注点[：:]\s*(.+?)(?:\n|$)", text)
    if m:
        question = m.group(1).strip()

    return code, name, question


def _prior_stage_notes(messages: list[BaseMessage]) -> str:
    notes: list[str] = []
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        text = message_text(msg)
        if text.startswith("【阶段"):
            notes.append(text)
    return "\n\n".join(notes)


def _build_tool_agent(
    model: ChatOpenAI,
    *,
    name: str,
    system_prompt: str,
    tools: list[Any],
) -> CompiledStateGraph:
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        name=name,
        middleware=list(MONITOR_MIDDLEWARE),
    )


def build_research_pipeline(model: ChatOpenAI) -> CompiledStateGraph:
    """显式六维分析 LangGraph 流水线，供 CompiledSubAgent 挂载。"""

    agent_fundamentals = _build_tool_agent(
        model,
        name="fundamentals_agent",
        system_prompt=STAGE1_PROMPT,
        tools=[get_stock_profile, get_stock_overview, get_stock_quote, get_stock_finance],
    )
    agent_peers = _build_tool_agent(
        model,
        name="valuation_agent",
        system_prompt=STAGE2_PROMPT,
        tools=[get_stock_finance],
    )
    agent_boards = _build_tool_agent(
        model,
        name="boards_agent",
        system_prompt=STAGE3_PROMPT,
        tools=[
            get_board_resonance,
            compare_board_fundamentals,
            web_search,
        ],
    )
    agent_technical = _build_tool_agent(
        model,
        name="technical_agent",
        system_prompt=STAGE4_PROMPT,
        tools=[get_technical_analysis],
    )
    agent_intel = _build_tool_agent(
        model,
        name="intel_agent",
        system_prompt=STAGE5_PROMPT,
        tools=[
            search_company_news,
            web_search,
            search_policy_impact,
            search_macro_international,
        ],
    )

    async def init_brief(state: ResearchState) -> dict[str, Any]:
        raw = ""
        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, HumanMessage):
                raw = message_text(msg)
                break
        if not raw and state.get("messages"):
            raw = message_text(state["messages"][0])
        code, name, question = parse_brief(raw)
        logger.info(
            "流水线解析任务：%s（%s），关注点：%s",
            name or code or "未知",
            code or "-",
            (question or "全面分析")[:80],
        )
        return {
            "code": code,
            "name": name,
            "question": question,
            "messages": [
                AIMessage(
                    content=(
                        f"【阶段0·解析】已锁定标的 {name or code}（{code or '未知'}）；"
                        f"关注点：{question or '财务与估值优先'}"
                    )
                )
            ],
        }

    async def run_tool_stage(
        state: ResearchState,
        *,
        agent: CompiledStateGraph,
        stage_tag: str,
        stage_hint: str,
        recursion_limit: int,
    ) -> dict[str, Any]:
        code = state.get("code") or ""
        name = state.get("name") or ""
        question = state.get("question") or ""
        prior = _prior_stage_notes(list(state.get("messages") or []))
        human = build_stage_user_message(
            code=code,
            name=name,
            question=question,
            stage_hint=stage_hint,
            prior_notes=prior,
        )
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=human)]},
            config={"recursion_limit": recursion_limit},
        )
        summary = _last_ai_text(list(result.get("messages") or []))
        if not summary:
            summary = "本阶段未产出有效摘要。"
        return {"messages": [AIMessage(content=f"【{stage_tag}】\n{summary}")]}

    async def stage_1_fundamentals(state: ResearchState) -> dict[str, Any]:
        return await run_tool_stage(
            state,
            agent=agent_fundamentals,
            stage_tag="阶段1·基本面画像",
            stage_hint="请完成本阶段任务：拉取档案与财务概览；财务写详细；若有业绩预告必须单独写清区间与正式披露预约日。",
            recursion_limit=8,
        )

    async def stage_2_peers(state: ResearchState) -> dict[str, Any]:
        return await run_tool_stage(
            state,
            agent=agent_peers,
            stage_tag="阶段2·股性与估值框架",
            stage_hint=(
                "请完成本阶段任务：判定成长/周期/价值/混合；"
                "在股性框架下给出估值偏贵/合理/偏低的框架结论。"
                "同业对比放到下一阶段，本阶段不要拉同业名单。"
            ),
            recursion_limit=6,
        )

    async def stage_3_boards(state: ResearchState) -> dict[str, Any]:
        return await run_tool_stage(
            state,
            agent=agent_boards,
            stage_tag="阶段3·板块联动与同业对比",
            stage_hint=(
                "请完成本阶段任务：必须 get_board_resonance（仅行业，不要概念联动）；"
                "必须 compare_board_fundamentals 做成分股业绩/PE/PB/现金流横向对比；"
                "必须 web_search 检索行业景气与动态；给出板块内相对价值结论。"
            ),
            recursion_limit=12,
        )

    async def stage_4_technical(state: ResearchState) -> dict[str, Any]:
        return await run_tool_stage(
            state,
            agent=agent_technical,
            stage_tag="阶段4·技术面",
            stage_hint=(
                "请完成本阶段任务：调用 get_technical_analysis 做技术面辅助观察。"
            ),
            recursion_limit=6,
        )

    async def stage_5_intel(state: ResearchState) -> dict[str, Any]:
        return await run_tool_stage(
            state,
            agent=agent_intel,
            stage_tag="阶段5·情报补缺",
            stage_hint=(
                "请完成本阶段任务：必须先 search_company_news 检索该公司最新公告与新闻；"
                "再按缺口补充政策/宏观；输出要点带来源。"
            ),
            recursion_limit=10,
        )

    async def stage_6_report(state: ResearchState) -> dict[str, Any]:
        code = state.get("code") or ""
        name = state.get("name") or ""
        question = state.get("question") or ""
        prior = _prior_stage_notes(list(state.get("messages") or []))
        human = build_report_user_message(
            code=code,
            name=name,
            question=question,
            prior_notes=prior,
        )
        resp = await model.ainvoke(
            [
                SystemMessage(content=STAGE6_PROMPT),
                HumanMessage(content=human),
            ]
        )
        report = message_text(resp)
        if not report:
            report = "未能生成综合报告。"
        return {"messages": [AIMessage(content=report)]}

    graph = StateGraph(ResearchState)
    graph.add_node("init_brief", init_brief)
    graph.add_node("stage_1_fundamentals", stage_1_fundamentals)
    graph.add_node("stage_2_peers", stage_2_peers)
    graph.add_node("stage_3_boards", stage_3_boards)
    graph.add_node("stage_4_technical", stage_4_technical)
    graph.add_node("stage_5_intel", stage_5_intel)
    graph.add_node("stage_6_report", stage_6_report)

    graph.add_edge(START, "init_brief")
    graph.add_edge("init_brief", "stage_1_fundamentals")
    graph.add_edge("stage_1_fundamentals", "stage_2_peers")
    graph.add_edge("stage_2_peers", "stage_3_boards")
    graph.add_edge("stage_3_boards", "stage_4_technical")
    graph.add_edge("stage_4_technical", "stage_5_intel")
    graph.add_edge("stage_5_intel", "stage_6_report")
    graph.add_edge("stage_6_report", END)

    compiled = graph.compile(name=PIPELINE_SUBAGENT_NAME)
    logger.info("六维分析流水线已编译：%s", PIPELINE_SUBAGENT_NAME)
    return compiled
