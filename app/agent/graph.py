from __future__ import annotations

import logging
from functools import lru_cache

from deepagents import CompiledSubAgent, create_deep_agent
from deepagents.backends import StateBackend
from deepagents.profiles import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain_openai import ChatOpenAI
from langgraph.graph.state import CompiledStateGraph

from app.agent.middleware import MONITOR_MIDDLEWARE
from app.agent.pipeline import build_research_pipeline
from app.agent.prompts import ORCHESTRATOR_PROMPT
from app.agent.stages import PIPELINE_SUBAGENT_NAME
from app.core.config import get_settings
from app.persistence.checkpointer import get_checkpointer

logger = logging.getLogger(__name__)

# 关闭默认 general-purpose，避免自由 task 打乱六维分析
_GP_OFF = HarnessProfile(
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    excluded_tools=frozenset({"execute"}),
)
for _key in ("openai", "deepseek", "anthropic"):
    register_harness_profile(_key, _GP_OFF)


def build_chat_model(*, temperature: float = 0.3) -> ChatOpenAI:
    settings = get_settings()
    settings.require_llm()
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=temperature,
        timeout=120,
        max_retries=2,
    )


@lru_cache
def get_fundamental_agent() -> CompiledStateGraph:
    """
    主编排（deep agent）+ 六维分析流水线（LangGraph CompiledSubAgent）。

    业务分析顺序写死在 pipeline 图内，不再自由双智能体 task。
    """
    model = build_chat_model()
    pipeline = build_research_pipeline(model)
    research_crew: CompiledSubAgent = {
        "name": PIPELINE_SUBAGENT_NAME,
        "description": (
            "A股六维分析（唯一分析入口）："
            "①基本面财务画像 → ②股性与估值框架 → "
            "③板块联动与同业对比 → ④技术面辅助 → "
            "⑤情报补缺 → ⑥固定大纲综合报告。"
            "分析任意股票时必须调用且仅调用一次。"
        ),
        "runnable": pipeline,
    }
    logger.info("构建深研 Agent，子流水线=%s", PIPELINE_SUBAGENT_NAME)
    return create_deep_agent(
        model=model,
        tools=[],
        system_prompt=ORCHESTRATOR_PROMPT,
        backend=StateBackend(),
        name="a_share_multi_agent",
        subagents=[research_crew],
        middleware=MONITOR_MIDDLEWARE,
        checkpointer=get_checkpointer(),
    )


def reset_fundamental_agent() -> None:
    """配置/连接重置后丢弃缓存的深研 Agent。"""
    get_fundamental_agent.cache_clear()
