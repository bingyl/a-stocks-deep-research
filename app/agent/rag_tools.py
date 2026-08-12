"""追问 Agent 用的 RAG 召回工具（BaseRetriever + create_retriever_tool）。"""

from __future__ import annotations

from langchain_core.prompts import PromptTemplate
from langchain_core.tools import create_retriever_tool

from app.core.config import get_settings
from app.rag.retriever import ReportKnowledgeRetriever


def _rag_description() -> str:
    backend = (get_settings().vector_backend or "chroma").strip().lower()
    if backend == "milvus":
        recall = "稠密向量 + BM25（若服务端可用）双路，RRF 融合"
    else:
        recall = "稠密向量召回（当前 Chroma 无 BM25，勿假设关键词必中）"

    return f"""从本报告分析过程沉淀的知识库召回（{recall}）。

适用：回答依赖「此前已查过的财务/新闻/板块材料」的追问（如现金流、营收、公告摘要）。
返回文本开头是精简 JSON summary（hint / freshness / items 索引），其后为去重后的父文档正文。

使用规则（仅供内部决策，禁止写入给用户的回答）：
1. 追问时优先且单独先调本工具，不要与 get_stock_finance 等并行。
2. 若 hint 写明已有未过期财务材料，且正文含所需数字：直接作答，禁止再调 get_stock_finance / get_stock_overview。
3. 若 hint 提示命中偏新闻、缺少财务数字：应补调 get_stock_finance，勿只凭新闻推断财报。
4. 仅当 requires_web_refresh=true 且问题是新闻/公告时，再调 web_search / search_company_news。
5. 仅用户明确要实时最新价时才调 get_stock_quote。
6. 回答用户时只写投研结论与业务来源，不要复述 fresh_count / hint 等字段。
"""


_DOCUMENT_PROMPT = PromptTemplate.from_template("{page_content}")


def build_rag_search_tool(*, top_k: int = 5):
    """包装 ParentChild 混合检索为标准 LangChain retriever tool。"""
    retriever = ReportKnowledgeRetriever(top_k=top_k)
    return create_retriever_tool(
        retriever,
        name="rag_search",
        description=_rag_description(),
        document_prompt=_DOCUMENT_PROMPT,
        document_separator="\n\n---\n\n",
    )


rag_search = build_rag_search_tool()
RAG_TOOLS = [rag_search]
