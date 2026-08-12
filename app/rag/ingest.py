"""工具产出 -> 父子切片 -> embedding -> ParentChildIndex。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from app.core.config import get_settings
from app.core.logging import log_caught
from app.integrations import embedding as embedding_svc
from app.rag.chunking import WEB_TOOLS, build_parent_units, parse_tool_output, split_to_parent_child
from app.rag.kb_lookup import (
    list_ids_with_prefix,
    parents_content_unchanged,
    tool_arg_key,
    tool_parent_prefix,
)
from app.rag.util import preview_text
from app.services import analysis_jobs
from app.services.reports import STATUS_CANCELLED, get_report_status
from app.persistence.vectorstore.factory import get_parent_child_index
from app.persistence.vectorstore.types import ChildDocument, ParentDocument

logger = logging.getLogger(__name__)

_SOURCE_LABELS = {
    "json": "JSON",
    "web": "网页文本",
    "text": "文本",
}

# 入库白名单：情报 + 财经类；跳过档案短结果
INGEST_TOOLS = WEB_TOOLS | {
    "get_stock_quote",
    "get_stock_finance",
    "get_stock_overview",
    "get_kline",
    "get_technical_analysis",
    "get_industry_peers",
    "get_peer_valuation",
    "get_board_resonance",
    "get_board_members",
    "compare_board_fundamentals",
}

_ready_lock = threading.Lock()
_bg_tasks: set[asyncio.Task[Any]] = set()
_ingest_inflight: set[str] = set()
_ingest_gate = threading.Lock()


async def _should_skip_ingest(report_id: str, analysis_run_id: str = "") -> bool:
    """取消 / 报告已删 / 被新一轮重跑 supersede 后的迟到入库应跳过。"""
    if not report_id:
        return False
    try:
        rid = int(report_id)
        status = await get_report_status(rid)
        if status is None:
            # 报告不存在（已删或错误 id）
            return True
        if status == STATUS_CANCELLED:
            return True
        if analysis_run_id and await analysis_jobs.is_superseded(rid, analysis_run_id):
            return True
        if analysis_run_id and analysis_jobs.is_cancel_requested(analysis_run_id):
            return True
        return False
    except Exception as exc:
        log_caught(
            logger,
            "check ingest skip failed id=%s run=%s",
            report_id,
            analysis_run_id or "-",
            exc=exc,
            level=logging.DEBUG,
        )
        return False


def reset_ingest_state() -> None:
    """配合 reset_vector_store：清理后台入库任务引用。"""
    with _ready_lock:
        _bg_tasks.clear()
    with _ingest_gate:
        _ingest_inflight.clear()


def _ingest_flight_key(
    report_id: str, tool: str, arguments: dict[str, Any] | None
) -> str:
    return f"{report_id}:{tool}:{tool_arg_key(arguments)}"


def _try_begin_ingest(key: str) -> bool:
    with _ingest_gate:
        if key in _ingest_inflight:
            return False
        _ingest_inflight.add(key)
        return True


def _end_ingest(key: str) -> None:
    with _ingest_gate:
        _ingest_inflight.discard(key)


def _ensure_collection() -> str:
    settings = get_settings()
    name = settings.rag_collection
    with _ready_lock:
        get_parent_child_index().ensure(name)
        return name


def _is_error_output(parsed: Any) -> bool:
    if isinstance(parsed, dict) and parsed.get("error"):
        return True
    if isinstance(parsed, str) and "error" in parsed[:80].lower():
        try:
            obj = json.loads(parsed)
            if isinstance(obj, dict) and obj.get("error"):
                return True
        except json.JSONDecodeError:
            pass
    return False


def _normalize_output(output: Any) -> str | Any:
    if hasattr(output, "content"):
        return getattr(output, "content")
    return output


def purge_report_rag(report_id: str) -> None:
    """按 report_id 清理旧子块与父文档（删除报告 / 重跑前调用）。"""
    settings = get_settings()
    if not settings.rag_ingest_enabled() or not report_id:
        logger.debug("跳过知识库清理（RAG 关闭或空 report_id）：%s", report_id or "-")
        return
    try:
        collection = _ensure_collection()
        idx = get_parent_child_index()
        n_child = idx.delete_children(collection, where={"report_id": str(report_id)})
        prefix = f"{report_id}:"
        parent_ids = idx.doc_store.list_ids(collection, key_prefix=prefix)
        n_parent = idx.delete_parents(collection, parent_ids) if parent_ids else 0
        logger.info(
            "清理报告知识库：报告#%s，删除子片段 %s、父片段 %s",
            report_id,
            n_child,
            n_parent,
        )
    except Exception as exc:
        log_caught(
            logger,
            "清理报告知识库失败：报告#%s",
            report_id,
            exc=exc,
            level=logging.ERROR,
        )


def _prepare_ingest_docs(
    *,
    report_id: str,
    code: str,
    tool: str,
    arguments: dict[str, Any] | None,
    output: Any,
    stage: str = "",
) -> tuple[list[ParentDocument], list[ChildDocument]] | None:
    """切片准备；返回 (parents, children) 或 None（跳过）。"""
    settings = get_settings()
    if not settings.rag_ingest_enabled():
        return None
    if tool not in INGEST_TOOLS:
        logger.debug("RAG 跳过入库（工具不在白名单）：%s", tool)
        return None
    if not report_id:
        return None

    raw = _normalize_output(output)
    parsed = parse_tool_output(raw)
    if isinstance(parsed, dict) and parsed.get("skipped"):
        logger.info(
            "RAG 跳过入库（工具已短路）：%s，报告#%s，原因：%s",
            tool,
            report_id,
            preview_text(parsed.get("reason"), 120),
        )
        return None
    if parsed is None or _is_error_output(parsed):
        logger.debug("RAG 跳过入库（空结果或错误）：工具=%s，报告#%s", tool, report_id)
        return None

    units = build_parent_units(
        tool,
        arguments,
        parsed,
        report_id=report_id,
        code=code or "",
        stage=stage or "",
    )
    if not units:
        logger.debug("RAG 跳过入库（无可切分内容）：工具=%s", tool)
        return None

    parents, children = split_to_parent_child(units)
    if not children:
        return None
    return parents, children


def _log_ingest_done(
    *,
    tool: str,
    parents: list[ParentDocument],
    children: list[ChildDocument],
    result: dict[str, int],
    elapsed_ms: float,
    sync: bool = False,
) -> None:
    source = (
        str((parents[0].metadata or {}).get("source_type") or "text") if parents else "text"
    )
    kind = _SOURCE_LABELS.get(source, source)
    char_count = sum(len(p.text or "") for p in parents)
    sample = preview_text(parents[0].text if parents else "", 120)
    mode = "（同步）" if sync else ""
    n_parent = result.get("parents", len(parents))
    n_child = result.get("children", len(children))
    # 网页：一条检索结果一个父片段，勿按总字数理解「该合成 1 个」
    if source == "web":
        logger.info(
            "RAG %s切分%s：工具 %s，检索条目 %s 条（合计约 %s 字符），"
            "父片段 %s，子片段 %s，已写入知识库（%.1f 秒）｜预览：%s",
            kind,
            mode,
            tool,
            len(parents),
            char_count,
            n_parent,
            n_child,
            elapsed_ms / 1000.0,
            sample,
        )
        return
    logger.info(
        "RAG %s切分%s：工具 %s，共约 %s 字符，父片段 %s，子片段 %s，已写入知识库（%.1f 秒）｜预览：%s",
        kind,
        mode,
        tool,
        char_count,
        n_parent,
        n_child,
        elapsed_ms / 1000.0,
        sample,
    )


def _purge_stale_tool_prefix(
    *,
    report_id: str,
    tool: str,
    arguments: dict[str, Any] | None,
    keep_parent_ids: set[str],
) -> None:
    """同工具同参数重入库时，删除本次未覆盖的旧父/子片段，避免孤儿召回。"""
    if not report_id or not tool:
        return
    collection = _ensure_collection()
    idx = get_parent_child_index()
    prefix = tool_parent_prefix(report_id, tool, arguments)
    old_ids = list_ids_with_prefix(collection, prefix)
    stale = [i for i in old_ids if i not in keep_parent_ids]
    if not stale:
        return
    # 先删子向量（按 parent_id），再删父文档
    for pid in stale:
        try:
            idx.delete_children(collection, where={"parent_id": pid})
        except Exception as exc:
            log_caught(
                logger,
                "删除孤儿子片段失败 parent_id=%s",
                pid,
                exc=exc,
                level=logging.DEBUG,
            )
    n = idx.delete_parents(collection, stale)
    logger.info(
        "RAG 清理孤儿片段：工具 %s，报告#%s，删除旧父片段 %s",
        tool,
        report_id,
        n,
    )


def _upsert_ingest(
    parents: list[ParentDocument],
    children: list[ChildDocument],
    vectors: list[list[float]],
    *,
    report_id: str = "",
    tool: str = "",
    arguments: dict[str, Any] | None = None,
) -> dict[str, int]:
    filled: list[ChildDocument] = []
    for child, vec in zip(children, vectors):
        filled.append(
            ChildDocument(
                id=child.id,
                parent_id=child.parent_id,
                text=child.text,
                vector=vec,
                metadata=child.metadata,
            )
        )
    collection = _ensure_collection()
    idx = get_parent_child_index()
    if report_id and tool:
        _purge_stale_tool_prefix(
            report_id=report_id,
            tool=tool,
            arguments=arguments,
            keep_parent_ids={p.id for p in parents},
        )
    return idx.upsert(collection, parents=parents, children=filled)


async def aingest_tool_output(
    *,
    report_id: str,
    code: str,
    tool: str,
    arguments: dict[str, Any] | None,
    output: Any,
    stage: str = "",
    analysis_run_id: str = "",
) -> dict[str, int]:
    """异步入库：embedding 走 aembed_*，向量库写入放到线程池。"""
    empty = {"parents": 0, "children": 0}
    run_id = str(analysis_run_id or "")
    if await _should_skip_ingest(str(report_id or ""), run_id):
        logger.info(
            "RAG 跳过入库（已取消或已重跑）：工具 %s，报告#%s run=%s",
            tool,
            report_id,
            run_id or "-",
        )
        return empty
    t0 = time.perf_counter()
    prepared = await asyncio.to_thread(
        _prepare_ingest_docs,
        report_id=report_id,
        code=code,
        tool=tool,
        arguments=arguments,
        output=output,
        stage=stage,
    )
    if not prepared:
        return empty

    parents, children = prepared
    if await asyncio.to_thread(parents_content_unchanged, parents):
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "RAG 跳过重复入库：工具 %s，父片段 %s 已在知识库且内容未变（%.1f 秒）",
            tool,
            len(parents),
            elapsed / 1000.0,
        )
        return {"parents": 0, "children": 0, "skipped": 1}

    if await _should_skip_ingest(str(report_id or ""), run_id):
        logger.info(
            "RAG 跳过写入（已取消或已重跑）：工具 %s，报告#%s run=%s",
            tool,
            report_id,
            run_id or "-",
        )
        return empty

    texts = [c.text for c in children]
    vectors = await embedding_svc.aembed_texts(texts)
    if await _should_skip_ingest(str(report_id or ""), run_id):
        logger.info(
            "RAG 跳过写入（向量化后已取消或已重跑）：工具 %s，报告#%s run=%s",
            tool,
            report_id,
            run_id or "-",
        )
        return empty
    result = await asyncio.to_thread(
        _upsert_ingest,
        parents,
        children,
        vectors,
        report_id=report_id,
        tool=tool,
        arguments=arguments,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    _log_ingest_done(
        tool=tool,
        parents=parents,
        children=children,
        result=result,
        elapsed_ms=elapsed,
    )
    return result


def ingest_tool_output(
    *,
    report_id: str,
    code: str,
    tool: str,
    arguments: dict[str, Any] | None,
    output: Any,
    stage: str = "",
    analysis_run_id: str = "",
) -> dict[str, int]:
    """同步入库兜底（无事件循环时）。"""
    empty = {"parents": 0, "children": 0}
    from app.persistence.db.async_runner import run_coro

    if run_coro(_should_skip_ingest(str(report_id or ""), str(analysis_run_id or ""))):
        logger.info(
            "RAG 跳过入库（同步/已取消或已重跑）：工具 %s，报告#%s run=%s",
            tool,
            report_id,
            analysis_run_id or "-",
        )
        return empty
    t0 = time.perf_counter()
    prepared = _prepare_ingest_docs(
        report_id=report_id,
        code=code,
        tool=tool,
        arguments=arguments,
        output=output,
        stage=stage,
    )
    if not prepared:
        return empty
    parents, children = prepared
    if parents_content_unchanged(parents):
        elapsed = (time.perf_counter() - t0) * 1000
        logger.info(
            "RAG 跳过重复入库（同步）：工具 %s，父片段 %s 已在知识库且内容未变（%.1f 秒）",
            tool,
            len(parents),
            elapsed / 1000.0,
        )
        return {"parents": 0, "children": 0, "skipped": 1}

    vectors = embedding_svc.embed_texts([c.text for c in children])
    result = _upsert_ingest(
        parents,
        children,
        vectors,
        report_id=report_id,
        tool=tool,
        arguments=arguments,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    _log_ingest_done(
        tool=tool,
        parents=parents,
        children=children,
        result=result,
        elapsed_ms=elapsed,
        sync=True,
    )
    return result


def schedule_ingest(**kwargs: Any) -> None:
    """在事件循环中后台异步入库，不阻塞 SSE。"""
    settings = get_settings()
    if not settings.rag_ingest_enabled():
        return

    report_id = str(kwargs.get("report_id") or "")
    tool = str(kwargs.get("tool") or "")
    analysis_run_id = str(kwargs.get("analysis_run_id") or "")
    # 跳过检查在 aingest_tool_output 内 await 完成（此处多为已有事件循环）
    arguments = kwargs.get("arguments") if isinstance(kwargs.get("arguments"), dict) else {}
    flight = _ingest_flight_key(report_id, tool, arguments)
    if not _try_begin_ingest(flight):
        logger.info(
            "RAG 跳过重复调度：工具 %s，报告#%s（相同入库已在进行）",
            tool,
            report_id or "-",
        )
        return

    async def _run() -> None:
        try:
            await aingest_tool_output(**kwargs)
        except Exception as exc:
            log_caught(
                logger,
                "RAG 异步入库失败：工具=%s，报告#%s",
                kwargs.get("tool"),
                kwargs.get("report_id"),
                exc=exc,
                level=logging.ERROR,
            )
        finally:
            _end_ingest(flight)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:
        try:
            ingest_tool_output(**kwargs)
        except Exception as exc:
            log_caught(
                logger,
                "RAG 同步入库失败：工具=%s",
                kwargs.get("tool"),
                exc=exc,
                level=logging.ERROR,
            )
        finally:
            _end_ingest(flight)
