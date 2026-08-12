from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import AsyncIterator
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl, Runtime

from app.agent.graph import get_fundamental_agent
from app.agent.messages import message_text
from app.agent.prompts import build_user_prompt
from app.agent.sse_bridge import attach_sse_queue, drain_sse_queue, reset_sse_queue
from app.agent.stages import PIPELINE_SUBAGENT_NAME, STAGE_META
from app.agent.tools import _short, _tool_label
from app.core.config import get_settings
from app.core.logging import log_caught, reset_log_run_id, set_log_run_id
from app.rag.context import clear_rag_context, set_rag_context, update_rag_stage
from app.rag.ingest import purge_report_rag, schedule_ingest
from app.rag.util import extract_tool_output, preview_text
from app.services import analysis_jobs
from app.services import reports as reports_svc
from app.services.analysis_jobs import AnalysisCancelled
from app.services.stock import normalize_code
from app.services.universe import ensure_universe, suggest_stocks

logger = logging.getLogger(__name__)

# Pregel 将 Runtime 挂在 config["configurable"]["__pregel_runtime"]
_CONF = "configurable"
_CONFIG_KEY_RUNTIME = "__pregel_runtime"


def _run_config(
    recursion_limit: int,
    control: RunControl,
    *,
    thread_id: str,
) -> dict[str, Any]:
    """v2 astream_events 无 control=（仅 v3 有）；经 Runtime 注入以支持协作 drain。"""
    return {
        "recursion_limit": recursion_limit,
        _CONF: {
            "thread_id": thread_id,
            _CONFIG_KEY_RUNTIME: Runtime(control=control),
        },
    }


def _ensure_drain_if_cancelled(analysis_run_id: str | None, control: RunControl) -> bool:
    """若本轮已取消则确保 request_drain；返回是否处于取消/drain 中。"""
    if not analysis_jobs.is_cancel_requested(analysis_run_id):
        return False
    if not control.drain_requested:
        control.request_drain("user_cancel")
        logger.info(
            "检查点读到取消信号，RunControl.request_drain：run=%s",
            analysis_run_id or "-",
        )
    return True


async def _purge_rag_after_cancel(
    report_id: int | None,
    analysis_run_id: str | None,
) -> None:
    """仅当本轮仍是报告当前分析时，才清库（避免旧轮 drain 误删新一轮入库）。"""
    if not report_id:
        return
    if not await analysis_jobs.owns_report(report_id, analysis_run_id):
        logger.info(
            "跳过取消清库：报告#%s 已由更新的分析接管（旧 run=%s，当前=%s）",
            report_id,
            analysis_run_id or "-",
            await analysis_jobs.current_run_id(report_id) or "-",
        )
        return
    try:
        purge_report_rag(str(int(report_id)))
    except Exception as exc:
        log_caught(logger, "取消后清理知识库失败 id=%s", report_id, exc=exc, level=logging.ERROR)


async def _apply_terminal_if_owner(
    report_id: int | None,
    analysis_run_id: str | None,
    *,
    kind: str,
    message: str,
) -> bool:
    """旧轮次被重跑 supersede 后，禁止再改报告状态。"""
    if not report_id:
        return False
    if not await analysis_jobs.owns_report(report_id, analysis_run_id):
        logger.info(
            "跳过%s：报告#%s 已不属于 run=%s（当前=%s）",
            kind,
            report_id,
            analysis_run_id or "-",
            await analysis_jobs.current_run_id(report_id) or "-",
        )
        return False
    try:
        if kind == "cancelled":
            await reports_svc.mark_report_cancelled(report_id, message=message)
        else:
            await reports_svc.mark_report_error(report_id, message)
    except Exception as exc:
        log_caught(
            logger,
            "mark report %s failed id=%s run=%s",
            kind,
            report_id,
            analysis_run_id,
            exc=exc,
            level=logging.ERROR,
        )
        return False
    return True


async def resolve_stock_name(code: str) -> Optional[str]:
    code = normalize_code(code)
    await ensure_universe()
    items, _ = await suggest_stocks(code, limit=5)
    for item in items:
        if item.code == code:
            return item.name
    return None


def _extract_tool_trace(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    tool_results: dict[str, str] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_results[str(msg.tool_call_id)] = message_text(msg)[:500]

    trace: list[dict[str, Any]] = []
    round_idx = 0
    for msg in messages:
        if not isinstance(msg, AIMessage) or not msg.tool_calls:
            continue
        round_idx += 1
        for tc in msg.tool_calls:
            tc_id = str(tc.get("id") or "")
            trace.append(
                {
                    "round": round_idx,
                    "tool": tc.get("name") or "",
                    "arguments": tc.get("args") or {},
                    "result_preview": tool_results.get(tc_id, ""),
                }
            )
    return trace


def _final_analysis(messages: list[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and not msg.tool_calls:
            text = message_text(msg)
            if text:
                return text
    return "未能生成分析结果，请稍后重试。"


def _format_args(tool_name: str, inputs: dict[str, Any]) -> str:
    if not inputs:
        return ""
    if tool_name == "task":
        sub = inputs.get("subagent_type") or ""
        desc = _short(inputs.get("description"), 48)
        parts = []
        if sub:
            parts.append(f"六维={sub}")
        if desc:
            parts.append(f"任务={desc}")
        return " · ".join(parts)

    preferred = ["code", "query", "topic", "period", "limit", "count", "freshness"]
    parts: list[str] = []
    for key in preferred:
        if key in inputs and inputs[key] not in (None, ""):
            parts.append(f"{key}={_short(inputs[key], 36)}")
    return " · ".join(parts)


def _resolve_stage_node(metadata: dict[str, Any], name: str) -> str | None:
    node = str(metadata.get("langgraph_node") or "")
    if node in STAGE_META:
        return node
    if name in STAGE_META:
        return name
    # checkpoint ns 里可能带节点名
    ns = str(metadata.get("langgraph_checkpoint_ns") or "")
    for key in STAGE_META:
        if key in ns or key in node:
            return key
    return None


def _stage_payload(node: str, status: str) -> dict[str, Any]:
    meta = STAGE_META[node]
    idx = meta["index"]
    total = meta["total"]
    title = meta["title"]
    detail = meta["detail"]
    if status == "start":
        if idx == 0:
            message = f"准备中 · {title}：{detail}"
        else:
            message = f"阶段 {idx}/{total} · {title}：{detail}"
    else:
        if idx == 0:
            message = f"完成 · {title}"
        else:
            message = f"阶段 {idx}/{total} 完成 · {title}"
    return {
        "stage": node,
        "index": idx,
        "total": total,
        "title": title,
        "detail": detail,
        "status": status,
        "message": message,
    }


async def analyze_fundamentals(
    code: str,
    question: Optional[str] = None,
) -> dict[str, Any]:
    final: dict[str, Any] | None = None
    async for event in stream_analyze_fundamentals(code, question):
        if event.get("event") == "final":
            final = event.get("data") or {}
    if not final:
        raise RuntimeError("分析未返回最终结果")
    return final


async def stream_analyze_fundamentals(
    code: str,
    question: Optional[str] = None,
    report_id: int | None = None,
    user_id: int | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    SSE：stage / status / tool_start / tool_end / final / error / cancelled
    默认新建 running 记录；传入 report_id 时在原任务（取消/失败）上重跑。
    """
    settings = get_settings()
    code = normalize_code(code)
    name = await resolve_stock_name(code)
    model_name = settings.llm_model
    recursion_limit = max(60, settings.agent_max_tool_rounds * 10)
    framework = "deepagents+CompiledSubAgent+langgraph-pipeline"

    created_at = ""
    reused = False
    analysis_run_id = analysis_jobs.new_run_id()
    log_token = set_log_run_id(analysis_run_id)
    sse_queue, sse_token = attach_sse_queue()
    run_control: RunControl | None = None
    start_msg = f"开始分析 {name or code}（{code}），启动六维分析…"
    try:
        if report_id:
            reused = True
            start_msg = f"在原任务 #{int(report_id)} 上重新分析 {name or code}（{code}）…"
            pending = await reports_svc.reactivate_report(
                int(report_id),
                model=model_name,
                question=question,
                framework=framework,
                message=start_msg,
                analysis_run_id=analysis_run_id,
                user_id=user_id,
            )
            report_id = int(pending["id"])
            created_at = str(pending.get("created_at") or "")
            name = pending.get("name") or name
        else:
            pending = await reports_svc.create_pending_report(
                code=code,
                name=name,
                question=question,
                model=model_name,
                framework=framework,
                message=start_msg,
                analysis_run_id=analysis_run_id,
                user_id=user_id,
            )
            report_id = int(pending["id"])
            created_at = str(pending.get("created_at") or "")
        # 当前轮次已写入 DB；内存只挂 RunControl
        run_control = analysis_jobs.register(report_id, run_id=analysis_run_id)
    except reports_svc.ActiveAnalysisExistsError:
        reset_sse_queue(sse_token)
        reset_log_run_id(log_token)
        raise
    except reports_svc.ReportRestartError:
        reset_sse_queue(sse_token)
        reset_log_run_id(log_token)
        raise
    except Exception as exc:
        log_caught(
            logger,
            "prepare research report failed code=%s report_id=%s reused=%s",
            code,
            report_id,
            reused,
            exc=exc,
            level=logging.ERROR,
        )
        reset_sse_queue(sse_token)
        reset_log_run_id(log_token)
        raise

    yield {
        "event": "status",
        "data": {
            "step": "init",
            "message": start_msg,
            "code": code,
            "name": name,
            "report_id": report_id,
            "analysis_run_id": analysis_run_id,
            "created_at": created_at,
            "status": "running" if report_id else "",
            "reused": reused,
        },
    }

    agent = get_fundamental_agent()
    user_prompt = build_user_prompt(code, name, question)
    assert run_control is not None
    thread_id = (
        f"analysis:r{int(report_id or 0)}:{analysis_run_id}"
        if analysis_run_id
        else f"analysis:r{int(report_id or 0)}:anon"
    )
    run_config = _run_config(recursion_limit, run_control, thread_id=thread_id)
    logger.info(
        "开始深研分析：%s（%s），模型=%s，流水线=%s，报告#%s，run=%s，thread=%s",
        name or code,
        code,
        model_name,
        PIPELINE_SUBAGENT_NAME,
        report_id or "-",
        analysis_run_id or "-",
        thread_id,
    )

    messages: list[BaseMessage] = []
    seen_tool_runs: set[str] = set()
    seen_stage_events: set[str] = set()
    tool_seq = 0
    tool_inputs_by_run: dict[str, dict[str, Any]] = {}
    current_stage = ""

    rag_on = get_settings().rag_ingest_enabled()
    if report_id and rag_on:
        set_rag_context(str(report_id), code=code, name=name or "")
        try:
            purge_report_rag(str(report_id))
        except Exception as exc:
            log_caught(logger, "purge_report_rag failed id=%s", report_id, exc=exc, level=logging.ERROR)

    try:
        logger.info(
            "开始推送 Agent 事件流：报告#%s run=%s",
            report_id or "-",
            analysis_run_id or "-",
        )
        first_agent_event = True
        async for event in agent.astream_events(
                input={"messages": [{"role": "user", "content": user_prompt}]},
                config=run_config,
                version="v2",
        ):
            # 中间件旁路事件并入 SSE（先于本轮 LangGraph 事件）
            for item in drain_sse_queue(sse_queue, analysis_run_id=analysis_run_id):
                data = dict(item.get("data") or {})
                data.setdefault("report_id", report_id)
                data.setdefault("analysis_run_id", analysis_run_id)
                yield {"event": item["event"], "data": data}

            # 取消 API 已 request_drain；此处再确认，并停止向 SSE 推送后续业务事件
            if _ensure_drain_if_cancelled(analysis_run_id, run_control):
                continue

            kind = event.get("event")
            tool_name = event.get("name") or ""
            data = event.get("data") or {}
            run_id = str(event.get("run_id") or "")
            metadata = event.get("metadata") or {}
            if first_agent_event:
                first_agent_event = False
                logger.info(
                    "收到首个 Agent 事件：%s %s，报告#%s run=%s",
                    kind or "-",
                    tool_name or "-",
                    report_id or "-",
                    analysis_run_id or "-",
                )

            stage_node = _resolve_stage_node(metadata, tool_name)
            if stage_node and kind in {"on_chain_start", "on_chain_end"}:
                status = "start" if kind == "on_chain_start" else "end"
                key = f"{stage_node}:{status}"
                if key not in seen_stage_events:
                    seen_stage_events.add(key)
                    payload = _stage_payload(stage_node, status)
                    stage_title = (
                        (STAGE_META.get(stage_node) or {}).get("title")
                        or payload.get("title")
                        or stage_node
                    )
                    logger.info(
                        "分析阶段「%s」%s",
                        stage_title,
                        "开始" if status == "start" else "结束",
                    )
                    if status == "start":
                        current_stage = stage_node
                        update_rag_stage(stage_node)
                    if report_id and await analysis_jobs.owns_report(report_id, analysis_run_id):
                        try:
                            await reports_svc.update_report_progress(
                                report_id,
                                message=payload.get("message"),
                                stage=payload,
                                kind="stage",
                            )
                        except Exception as exc:
                            log_caught(logger, "update report stage progress failed id=%s", report_id, exc=exc, level=logging.ERROR)
                    yield {
                        "event": "stage",
                        "data": {
                            **payload,
                            "report_id": report_id,
                            "analysis_run_id": analysis_run_id,
                        },
                    }

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
                detail = _format_args(tool_name, inputs)
                label = _tool_label(tool_name)
                msg = f"{label}" + (f"（{detail}）" if detail else "")
                if report_id and await analysis_jobs.owns_report(report_id, analysis_run_id):
                    try:
                        await reports_svc.update_report_progress(
                            report_id,
                            message=msg,
                            kind="tool",
                        )
                    except Exception as exc:
                        log_caught(logger, "update report tool progress failed id=%s", report_id, exc=exc, level=logging.ERROR)
                yield {
                    "event": "tool_start",
                    "data": {
                        "seq": tool_seq,
                        "tool": tool_name,
                        "message": msg,
                        "arguments": inputs,
                        "report_id": report_id,
                        "analysis_run_id": analysis_run_id,
                    },
                }

            elif kind == "on_tool_end":
                end_key = f"end:{run_id}" if run_id else ""
                if end_key and end_key in seen_tool_runs:
                    continue
                if end_key:
                    seen_tool_runs.add(end_key)
                label = _tool_label(tool_name)
                output = extract_tool_output(data if isinstance(data, dict) else {})
                args = tool_inputs_by_run.pop(run_id, {}) if run_id else {}
                logger.info(
                    "调用工具「%s」，参数：%s，结果：%s",
                    label,
                    preview_text(args, 300),
                    preview_text(output, 300),
                )
                if report_id and tool_name and rag_on:
                    schedule_ingest(
                        report_id=str(report_id),
                        code=code,
                        tool=tool_name,
                        arguments=args if isinstance(args, dict) else {},
                        output=output,
                        stage=current_stage,
                        analysis_run_id=analysis_run_id or "",
                    )
                yield {
                    "event": "tool_end",
                    "data": {
                        "tool": tool_name,
                        "message": label,
                        "report_id": report_id,
                    },
                }

            elif kind == "on_chain_end":
                output = data.get("output")
                if isinstance(output, dict) and output.get("messages"):
                    messages = list(output["messages"])

        if _ensure_drain_if_cancelled(analysis_run_id, run_control):
            raise AnalysisCancelled(report_id, run_id=analysis_run_id)

        if not messages:
            # 流已自然结束才允许兜底；取消路径绝不能再 ainvoke
            logger.warning("stream ended without messages, fallback ainvoke")
            # 1) 先读 checkpointer 已落盘状态，避免同 thread 盲续跑
            try:
                snap = await agent.aget_state(run_config)
                vals = getattr(snap, "values", None) or {}
                if isinstance(vals, dict) and vals.get("messages"):
                    messages = list(vals["messages"])
            except Exception as exc:
                log_caught(
                    logger,
                    "fallback aget_state failed run=%s",
                    analysis_run_id,
                    exc=exc,
                    level=logging.DEBUG,
                )
            if not messages:
                # 2) 新 thread 再跑，避免复用已半写入的 checkpoint
                fb_config = copy.deepcopy(run_config)
                fb_tid = f"{thread_id}:fallback"
                fb_config.setdefault(_CONF, {})["thread_id"] = fb_tid
                result = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": user_prompt}]},
                    config=fb_config,
                )
                messages = list(result.get("messages") or [])
            if _ensure_drain_if_cancelled(analysis_run_id, run_control):
                raise AnalysisCancelled(report_id, run_id=analysis_run_id)

        analysis = _final_analysis(messages)
        tool_trace = _extract_tool_trace(messages)
        payload = {
            "code": code,
            "name": name,
            "question": question,
            "model": model_name,
            "analysis": analysis,
            "tool_calls": tool_trace,
            "tool_rounds": len({t["round"] for t in tool_trace}) if tool_trace else 0,
            "framework": framework,
            "status": "done",
            "analysis_run_id": analysis_run_id,
        }
        if report_id:
            if not await analysis_jobs.owns_report(report_id, analysis_run_id):
                logger.info(
                    "跳过 finalize：报告#%s 已不属于 run=%s",
                    report_id,
                    analysis_run_id or "-",
                )
                payload["id"] = report_id
                payload["created_at"] = created_at
            else:
                try:
                    saved = await reports_svc.finalize_report(
                        report_id,
                        analysis=str(payload.get("analysis") or ""),
                        tool_calls=list(payload.get("tool_calls") or []),
                        tool_rounds=int(payload.get("tool_rounds") or 0),
                        framework=framework,
                        model=model_name,
                        name=name,
                        question=question,
                        message="六维分析完成，报告已生成",
                    )
                    if saved:
                        payload["id"] = saved["id"]
                        payload["created_at"] = saved["created_at"]
                        payload["status"] = saved.get("status") or "done"
                    else:
                        payload["id"] = report_id
                        payload["created_at"] = created_at
                except Exception as exc:
                    log_caught(
                        logger,
                        "finalize research report failed id=%s",
                        report_id,
                        exc=exc,
                        level=logging.ERROR,
                    )
                    payload["id"] = report_id
                    payload["created_at"] = created_at
        else:
            # 兜底：未创建 pending 时仍尝试直接保存
            try:
                saved = await reports_svc.save_report(
                    code=str(payload["code"]),
                    name=payload.get("name"),
                    question=payload.get("question"),
                    model=str(payload.get("model") or ""),
                    analysis=str(payload.get("analysis") or ""),
                    tool_calls=list(payload.get("tool_calls") or []),
                    tool_rounds=int(payload.get("tool_rounds") or 0),
                    framework=str(payload.get("framework") or ""),
                )
                payload["id"] = saved["id"]
                payload["created_at"] = saved["created_at"]
            except Exception as exc:
                log_caught(logger, "auto-save research report failed code=%s", code, exc=exc, level=logging.ERROR)

        logger.info(
            "深研分析完成：%s（%s），工具调用 %s 次，报告#%s run=%s",
            name or code,
            code,
            len(tool_trace),
            payload.get("id") or report_id or "-",
            analysis_run_id or "-",
        )
        yield {
            "event": "status",
            "data": {
                "step": "done",
                "message": "六维分析完成，正在输出报告…",
                "report_id": payload.get("id") or report_id,
                "analysis_run_id": analysis_run_id,
            },
        }
        yield {"event": "final", "data": payload}
    except GraphDrained as drained:
        # RunControl.request_drain → 图在下一 superstep 边界协作退出
        logger.info(
            "深研分析已 drain（%s）：%s，报告#%s run=%s",
            drained.reason,
            code,
            report_id or "-",
            analysis_run_id or "-",
        )
        await _apply_terminal_if_owner(
            report_id,
            analysis_run_id,
            kind="cancelled",
            message="用户取消了分析",
        )
        await _purge_rag_after_cancel(report_id, analysis_run_id)
        yield {
            "event": "cancelled",
            "data": analysis_jobs.cancel_payload(report_id, run_id=analysis_run_id),
        }
    except AnalysisCancelled:
        logger.info(
            "深研分析已取消：%s，报告#%s run=%s",
            code,
            report_id or "-",
            analysis_run_id or "-",
        )
        await _apply_terminal_if_owner(
            report_id,
            analysis_run_id,
            kind="cancelled",
            message="用户取消了分析",
        )
        await _purge_rag_after_cancel(report_id, analysis_run_id)
        yield {
            "event": "cancelled",
            "data": analysis_jobs.cancel_payload(report_id, run_id=analysis_run_id),
        }
    except asyncio.CancelledError:
        # 进程关闭 / Task 被显式取消。正常「断开 SSE」已由 iter_detached 隔离，不应走到这里。
        logger.warning(
            "深研分析协程被取消（服务关闭或任务被杀）：%s，报告#%s run=%s",
            code,
            report_id or "-",
            analysis_run_id or "-",
        )
        if await _apply_terminal_if_owner(
            report_id,
            analysis_run_id,
            kind="error",
            message="连接断开或服务重载，分析已中断（可重跑）",
        ):
            await _purge_rag_after_cancel(report_id, analysis_run_id)
        raise
    except Exception as exc:
        log_caught(logger, "stream analyze failed code=%s", code, exc=exc, level=logging.ERROR)
        if report_id and not analysis_jobs.is_cancel_requested(analysis_run_id):
            await _apply_terminal_if_owner(
                report_id,
                analysis_run_id,
                kind="error",
                message=f"分析失败: {exc}",
            )
        yield {
            "event": "error",
            "data": {
                "message": str(exc),
                "report_id": report_id,
                "analysis_run_id": analysis_run_id,
            },
        }
    finally:
        try:
            clear_rag_context()
        except Exception as exc:
            log_caught(
                logger,
                "clear_rag_context failed",
                exc=exc,
                level=logging.DEBUG,
            )
        analysis_jobs.unregister(analysis_run_id, report_id)
        reset_sse_queue(sse_token)
        reset_log_run_id(log_token)
