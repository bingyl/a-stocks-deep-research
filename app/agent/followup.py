from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.summarization import create_summarization_middleware
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph.state import CompiledStateGraph

from app.agent.followup_tools import wrap_followup_tools
from app.agent.graph import build_chat_model
from app.agent.messages import message_text
from app.agent.rag_tools import RAG_TOOLS
from app.agent.tools import ANALYST_TOOLS, INTEL_TOOLS, _short, _tool_label
from app.core.config import get_settings
from app.core.logging import log_caught
from app.rag.context import clear_rag_context, set_rag_context
from app.rag.ingest import schedule_ingest
from app.rag.kb_lookup import tool_arg_key
from app.rag.util import extract_tool_output, preview_text
from app.services import chat as chat_svc
from app.services import reports as reports_svc

logger = logging.getLogger(__name__)

REPORT_CONTEXT_LIMIT = 32000
HISTORY_LIMIT = 24

# 兼容旧引用：默认含 RAG（实际以 get_followup_tools() 为准）
FOLLOWUP_TOOLS = [*RAG_TOOLS, *ANALYST_TOOLS, *INTEL_TOOLS]


def get_followup_tools() -> list[Any]:
    """按配置组装追问工具列表（结构化工具带知识库去重短路）。"""
    base = wrap_followup_tools([*ANALYST_TOOLS, *INTEL_TOOLS])
    if get_settings().rag_followup_enabled():
        return [*RAG_TOOLS, *base]
    return base


def _followup_limits() -> tuple[int, int]:
    """返回 (tool_run_limit, model_run_limit)，均做合理夹逼。"""
    s = get_settings()
    tool_limit = max(4, min(int(s.followup_tool_run_limit or 16), 32))
    model_limit = max(tool_limit + 4, min(int(s.followup_model_run_limit or 24), 48))
    return tool_limit, model_limit


def _format_tool_args(tool_name: str, inputs: dict[str, Any]) -> str:
    if not inputs:
        return ""
    preferred = [
        "code",
        "query",
        "topic",
        "company",
        "period",
        "limit",
        "count",
        "freshness",
    ]
    parts: list[str] = []
    for key in preferred:
        if key in inputs and inputs[key] not in (None, ""):
            parts.append(f"{key}={_short(inputs[key], 36)}")
    if not parts and tool_name:
        # 兜底：取前两个非空参数
        for key, val in list(inputs.items())[:2]:
            if val not in (None, ""):
                parts.append(f"{key}={_short(val, 36)}")
    return " · ".join(parts)


FOLLOWUP_SYSTEM_WITH_RAG = """你是「A股深研」追问 Agent。用户已有一份针对某只 A 股的深研报告，你负责基于报告与工具继续答疑。

硬性规则：
1. 只回答与该股/A股投研相关的问题（含：总结/概括本报告、财务、估值、行业、技术面、公告新闻、政策影响等）。写代码、生活百科、闲聊等才拒绝；用户要「总结报告/提炼结论」必须作答，不得拒答。
2. 优先使用用户提供的「深研报告上下文」。报告已覆盖的结论直接引用，不要为了「再确认」重复拉数。
3. 检索顺序（内部决策，禁止写进回答）：
   - 第一步：只调用 rag_search。不要与 get_stock_finance / get_stock_overview 等同时发起。
   - 第二步：阅读工具返回的 hint / freshness / requires_web_refresh / 命中正文，自行决定是否还要调工具。
   - 有未过期财务材料且正文够用：直接作答，禁止再调 get_stock_finance / get_stock_overview。
   - 仅新闻/公告且 requires_web_refresh=true：再调 web_search 或 search_company_news。
   - 库中无财务材料且确需报表数字：才可单独调用 get_stock_finance（本轮最多一次）。
   - 仅用户明确要「实时最新价」时才调 get_stock_quote。
   - 工具返回 skipped=true：改用 rag_search 材料作答，勿换工具再拉。
4. 工具预算：单轮尽量 ≤3 次；多数追问应 1 次 rag_search 即答。
5. 对用户的最终回答必须是干净的中文 Markdown 投研内容；表格用标准 Markdown。
6. 【输出禁令】绝对不要在回答中写出：工具名、函数名、JSON 字段名、fresh_count、requires_web_refresh、skipped、hint、rag_search、知识库命中/时效/是否联网刷新、「直接作答」等内部推理过程；也不要用「---」分隔这类元信息。来源说明只写业务口径（如「一季报」「本地财务接口」「公开公告」），不要写后端实现细节。
7. 不做具体买卖指令，可给估值逻辑与风险提示。
"""

FOLLOWUP_SYSTEM_NO_RAG = """你是「A股深研」追问 Agent。用户已有一份针对某只 A 股的深研报告，你负责基于报告与工具继续答疑。

硬性规则：
1. 只回答与该股/A股投研相关的问题（含总结/概括本报告、财务、估值、行业、技术面、公告新闻等）。写代码、生活百科、闲聊等才拒绝；「总结报告」必须作答。
2. 优先使用用户提供的「深研报告上下文」。报告已覆盖的结论直接引用，不要为了「再确认」重复拉数。
3. 需要外部新闻/公告时直接 web_search 或 search_company_news；需要行情/财务用 get_stock_overview 等结构化工具。当前未启用知识库召回（无 rag_search）。
4. 工具预算：单轮尽量 ≤4 次。优先复合工具（get_stock_overview、compare_board_fundamentals）。拿不到的数据如实说「数据不足」。
5. 对用户的最终回答必须是干净的中文 Markdown；不要写出工具名、内部字段或决策推理过程。
6. 引用工具数据时注明业务口径与来源；不做具体买卖指令。
"""

# 兼容旧名
FOLLOWUP_SYSTEM = FOLLOWUP_SYSTEM_WITH_RAG


def _followup_system_prompt() -> str:
    if get_settings().rag_followup_enabled():
        return FOLLOWUP_SYSTEM_WITH_RAG
    return FOLLOWUP_SYSTEM_NO_RAG


CLASSIFY_PROMPT = """判断用户问题是否与「当前这只 A 股的深研追问」相关。

判定为 finance_related（相关）的包括但不限于：
- 总结/概括/提炼/解读「报告、分析、结论、要点、风险」
- 财务、估值、行情、行业、公告、新闻、政策对标的影响
- 对报告某段内容的追问、核对、补充

仅当明显无关时才输出 off_topic，例如：写代码、做饭、闲聊、与股票/报告完全无关的百科。
拿不准时一律输出 finance_related。

只输出一个词：
- finance_related
- off_topic

用户问题：
{question}
"""

# 明显与本报告/投研相关，跳过模型分类，避免误拒答
_REPORT_RELATED_RE = re.compile(
    r"(报告|深研|分析|结论|要点|风险|估值|财务|行情|公告|新闻|业绩|营收|利润|"
    r"现金流|PE|PB|同业|板块|总结|概括|提炼|解读|这份|上面|刚才|继续问|"
    r"这只股|该股|标的|股票|代码)",
    re.IGNORECASE,
)


def _truncate(text: str, limit: int = REPORT_CONTEXT_LIMIT) -> str:
    s = text or ""
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _final_analysis(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages or []):
        if not isinstance(msg, AIMessage):
            continue
        if getattr(msg, "tool_calls", None):
            continue
        text = message_text(msg)
        if text:
            return text
    return ""


def _is_obviously_report_related(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    return bool(_REPORT_RELATED_RE.search(q))


async def classify_question(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return "off_topic"
    if re.fullmatch(r"(你好|您好|在吗|哈哈+|呵呵+|谢谢|拜拜|再见)+", q):
        return "off_topic"
    # 「总结一下AI分析报告」这类必须放行，不能交给小模型误杀
    if _is_obviously_report_related(q):
        return "finance_related"
    try:
        model = build_chat_model(temperature=0)
        resp = await model.ainvoke(
            [HumanMessage(content=CLASSIFY_PROMPT.format(question=q[:800]))]
        )
        text = message_text(resp).lower()
        if "finance_related" in text:
            return "finance_related"
        if "off_topic" in text:
            return "off_topic"
        if "off" in text or "无关" in text:
            return "off_topic"
        return "finance_related"
    except Exception as exc:
        log_caught(logger, "classify_question failed, fallback finance_related", exc=exc, level=logging.ERROR)
        return "finance_related"


_META_LEAK_RE = re.compile(
    r"(fresh_count|requires_web_refresh|rag_search|skipped\s*=|get_stock_finance|"
    r"get_stock_overview|知识库命中|无需联网|联网刷新|直接作答|未过期命中|"
    r"hint\s*[=：]|freshness)",
    re.IGNORECASE,
)


def _sanitize_limit_answer(answer: str, *, tool_limit: int) -> str:
    """把中间件英文限额提示改成可读中文，避免直接展示给用户。"""
    text = (answer or "").strip()
    low = text.lower()
    if (
        "tool call limit reached" in low
        or "run limit exceeded" in low
        or "model call limit reached" in low
    ):
        return (
            f"本轮工具/模型调用已达上限（工具约 {tool_limit} 次）。"
            "我已尽量基于已查到的信息作答；若还需要更细数据，请把问题拆小一些"
            "（例如只问估值、只问近期公告、只问与某只股票对比），我再继续查。"
        )
    return text


def _sanitize_meta_leak(answer: str) -> str:
    """去掉模型把内部 RAG/工具决策写进正文的前缀。"""
    text = (answer or "").strip()
    if not text:
        return text

    # 常见形态：元推理 + --- + 正文
    for sep in ("\n---\n", "\n---\r\n", "\n——\n", "\n***\n"):
        if sep in text:
            head, tail = text.split(sep, 1)
            if _META_LEAK_RE.search(head) and tail.strip():
                text = tail.strip()
                break

    lines = text.splitlines()
    # 去掉开头连续「内部决策」行
    while lines and _META_LEAK_RE.search(lines[0]) and len(lines) > 1:
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and lines[0].strip() in {"---", "——", "***", "-"}:
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)

    cleaned = "\n".join(lines).strip()
    return cleaned or text


def _sanitize_followup_answer(answer: str, *, tool_limit: int) -> str:
    text = _sanitize_limit_answer(answer, tool_limit=tool_limit)
    return _sanitize_meta_leak(text)


def get_followup_agent() -> CompiledStateGraph:
    """轻量追问 Agent；是否挂载 rag_search 由配置决定。"""
    use_rag = get_settings().rag_followup_enabled()
    return _build_followup_agent(use_rag)


@lru_cache
def _build_followup_agent(use_rag_tool: bool) -> CompiledStateGraph:
    """按 use_rag_tool 缓存两套 Agent，避免改配置后工具列表错乱。"""
    tool_run_limit, model_run_limit = _followup_limits()
    if use_rag_tool:
        tools = get_followup_tools()
    else:
        tools = wrap_followup_tools([*ANALYST_TOOLS, *INTEL_TOOLS])
    system_prompt = (
        FOLLOWUP_SYSTEM_WITH_RAG if use_rag_tool else FOLLOWUP_SYSTEM_NO_RAG
    )
    model = build_chat_model(temperature=0.3)
    backend = StateBackend()
    summarization = create_summarization_middleware(model, backend)
    tool_limit = ToolCallLimitMiddleware(
        run_limit=tool_run_limit,
        exit_behavior="end",
    )
    model_limit = ModelCallLimitMiddleware(
        run_limit=model_run_limit,
        exit_behavior="end",
    )
    logger.info(
        "构建追问 Agent：工具 %s 个，知识库召回=%s，工具轮次上限=%s，模型轮次上限=%s",
        len(tools),
        "开" if use_rag_tool else "关",
        tool_run_limit,
        model_run_limit,
    )
    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        backend=backend,
        name="a_share_followup_agent",
        middleware=[summarization, tool_limit, model_limit],
    )


def _build_messages(
    report: dict[str, Any],
    history: list[dict[str, Any]],
    question: str,
) -> list[Any]:
    code = report.get("code") or ""
    name = report.get("name") or ""
    analysis = _truncate(str(report.get("analysis") or ""))
    context = (
        f"【深研报告上下文】\n"
        f"标的：{name}（{code}）\n\n"
        f"{analysis}\n\n"
        f"---\n"
        f"请结合上述报告回答。报告未覆盖或需最新数据时，调用工具补充；"
        f"默认股票代码为 {code}。"
    )
    msgs: list[Any] = [
        SystemMessage(content=context),
    ]
    for m in history:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            msgs.append(HumanMessage(content=content))
        elif role == "assistant":
            msgs.append(AIMessage(content=content))
    msgs.append(HumanMessage(content=question.strip()))
    return msgs


async def stream_followup_chat(report_id: int, question: str) -> AsyncIterator[dict[str, Any]]:
    """
    SSE：status / tool_start / tool_end / token / token_reset / final / error
    """
    settings = get_settings()
    q = (question or "").strip()
    if not q:
        yield {"event": "error", "data": {"message": "问题不能为空"}}
        return

    report = await reports_svc.get_report(report_id)
    if not report:
        yield {"event": "error", "data": {"message": "报告不存在"}}
        return

    yield {"event": "status", "data": {"message": "正在理解问题…"}}

    user_msg = chat_svc.add_message(
        report_id=report_id,
        role="user",
        content=q,
        refused=False,
        model="",
    )

    label = await classify_question(q)
    if label == "off_topic":
        assistant = chat_svc.add_message(
            report_id=report_id,
            role="assistant",
            content=chat_svc.REFUSAL_TEXT,
            refused=True,
            model=settings.llm_model,
        )
        yield {
            "event": "token",
            "data": {"text": chat_svc.REFUSAL_TEXT},
        }
        yield {
            "event": "final",
            "data": {
                "user_message": user_msg,
                "assistant_message": assistant,
                "refused": True,
            },
        }
        return

    yield {"event": "status", "data": {"message": "追问 Agent 思考中…"}}
    settings_rag = get_settings()
    if settings_rag.rag_followup_enabled() or settings_rag.rag_ingest_enabled():
        set_rag_context(
            str(report_id),
            code=str(report.get("code") or ""),
            name=str(report.get("name") or ""),
            stage="followup",
        )
    try:
        history = chat_svc.recent_messages(report_id, limit=HISTORY_LIMIT)
        if history and history[-1].get("id") == user_msg["id"]:
            history = history[:-1]

        tool_run_limit, model_run_limit = _followup_limits()
        agent = get_followup_agent()
        msgs = _build_messages(report, history, q)
        config = {"recursion_limit": max(48, model_run_limit * 4)}

        streamed = ""
        final_messages: list[BaseMessage] = []
        seen_tool_runs: set[str] = set()
        # 同一轮里相同工具+参数只记一次日志/入库（防止包装层或模型并发双触发）
        seen_tool_fp: set[str] = set()
        tool_seq = 0
        tool_inputs_by_run: dict[str, dict[str, Any]] = {}

        async for event in agent.astream_events(
            {"messages": msgs},
            version="v2",
            config=config,
        ):
            kind = event.get("event")
            tool_name = event.get("name") or "tool"
            data = event.get("data") or {}
            run_id = str(event.get("run_id") or "")

            if kind == "on_tool_start":
                if run_id and run_id in seen_tool_runs:
                    continue
                if run_id:
                    seen_tool_runs.add(run_id)
                inputs = data.get("input") if isinstance(data.get("input"), dict) else {}
                if not isinstance(inputs, dict):
                    inputs = {}
                if run_id:
                    tool_inputs_by_run[run_id] = inputs
                tool_seq += 1
                detail = _format_tool_args(tool_name, inputs)
                human = _tool_label(tool_name)
                msg = human + (f"（{detail}）" if detail else "")
                yield {
                    "event": "tool_start",
                    "data": {
                        "seq": tool_seq,
                        "tool": tool_name,
                        "label": human,
                        "message": msg,
                        "arguments": inputs,
                    },
                }
                yield {
                    "event": "status",
                    "data": {"message": f"正在{msg}"},
                }
            elif kind == "on_tool_end":
                end_key = f"end:{run_id}" if run_id else ""
                if end_key and end_key in seen_tool_runs:
                    continue
                if end_key:
                    seen_tool_runs.add(end_key)
                human = _tool_label(tool_name)
                output = extract_tool_output(data if isinstance(data, dict) else {})
                args = tool_inputs_by_run.pop(run_id, {}) if run_id else {}
                if not isinstance(args, dict):
                    args = {}
                # rag_search 按 query 去重；结构化工具按参数去重
                fp = f"{tool_name}:{tool_arg_key(args)}"
                if fp in seen_tool_fp:
                    logger.debug("忽略重复工具事件：%s", fp)
                    continue
                seen_tool_fp.add(fp)
                logger.info(
                    "调用工具「%s」，参数：%s，结果：%s",
                    human,
                    preview_text(args, 300),
                    preview_text(output, 300),
                )
                if (
                    tool_name
                    and tool_name != "rag_search"
                    and get_settings().rag_ingest_enabled()
                ):
                    schedule_ingest(
                        report_id=str(report_id),
                        code=str(report.get("code") or ""),
                        tool=tool_name,
                        arguments=args,
                        output=output,
                        stage="followup",
                        analysis_run_id=f"followup:{int(report_id)}",
                    )
                yield {
                    "event": "tool_end",
                    "data": {
                        "tool": tool_name,
                        "label": human,
                        "message": f"{human}完成",
                    },
                }
                yield {
                    "event": "status",
                    "data": {"message": f"{human}完成"},
                }
            elif kind == "on_chat_model_start":
                streamed = ""
                yield {"event": "token_reset", "data": {}}
                yield {
                    "event": "status",
                    "data": {"message": "正在生成回答…"},
                }
            elif kind == "on_chat_model_stream":
                chunk = data.get("chunk")
                if chunk is None:
                    continue
                if getattr(chunk, "tool_call_chunks", None):
                    continue
                piece = message_text(chunk)
                if not piece:
                    continue
                streamed += piece
                yield {"event": "token", "data": {"text": piece}}
            elif kind == "on_chain_end":
                output = data.get("output")
                if isinstance(output, dict) and output.get("messages"):
                    final_messages = list(output["messages"])

        answer = _final_analysis(final_messages) or streamed.strip() or "（未生成有效回答）"
        sanitized = _sanitize_followup_answer(answer, tool_limit=tool_run_limit)
        if sanitized != answer:
            # 流式可能已输出元推理/英文限额提示，重置后只保留干净正文
            yield {"event": "token_reset", "data": {}}
            yield {"event": "token", "data": {"text": sanitized}}
            answer = sanitized
        refused = "与投研无关" in answer or "只能围绕" in answer
        assistant = chat_svc.add_message(
            report_id=report_id,
            role="assistant",
            content=answer,
            refused=refused,
            model=settings.llm_model,
        )
        yield {
            "event": "final",
            "data": {
                "user_message": user_msg,
                "assistant_message": assistant,
                "refused": refused,
            },
        }
    except Exception as exc:
        log_caught(logger, "followup chat failed report_id=%s", report_id, exc=exc, level=logging.ERROR)
        msg = str(exc)
        if "limit" in msg.lower() and ("tool" in msg.lower() or "model" in msg.lower()):
            tool_run_limit, _ = _followup_limits()
            msg = _sanitize_limit_answer(msg, tool_limit=tool_run_limit)
        yield {"event": "error", "data": {"message": f"追问失败: {msg}"}}
    finally:
        clear_rag_context()
