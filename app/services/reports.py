from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, inspect, or_, select, text

from app.persistence.db import init_db, async_session_scope
from app.persistence.db.models import ResearchReport
from app.services.chat import (
    count_messages,
    count_messages_by_report_ids,
    delete_messages_for_report,
)
from app.services.run_ids import new_run_id

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_PENDING: "排队中",
    STATUS_RUNNING: "分析中",
    STATUS_DONE: "已完成",
    STATUS_ERROR: "失败",
    STATUS_CANCELLED: "已取消",
}

TERMINAL_STATUSES = {STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED}

_MAX_STEPS = 40


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _clip(text: str, n: int = 100) -> str:
    s = " ".join((text or "").split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _strip_md_inline(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"^#+\s*", "", s)
    return s.strip(" -\t")


def _preview(text: str, n: int = 100) -> str:
    """列表摘要：优先抽取「一句话结论」正文，去掉子智能体套话。"""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""

    m = re.search(
        r"(?:^|\n)\s*#{1,3}\s*1\.\s*一句话结论\s*\n+([\s\S]*?)(?=\n\s*#{1,3}\s*\d+\.|\Z)",
        raw,
        flags=re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"一句话结论\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s*#{1,3}\s*\d+\.|\Z)",
            raw,
        )
    body = (m.group(1) if m else raw).strip()

    skip_prefixes = (
        "以下为子智能体",
        "我原样呈现",
        "原样转述",
        "未删减",
        "---",
    )
    lines: list[str] = []
    for line in body.split("\n"):
        t = line.strip()
        if not t or t == "---":
            if lines:
                break
            continue
        if t.startswith("#"):
            if lines:
                break
            continue
        if any(t.startswith(p) for p in skip_prefixes):
            continue
        lines.append(_strip_md_inline(t))
        if len(" ".join(lines)) >= n:
            break

    conclusion = " ".join(lines).strip()
    if conclusion:
        return _clip(conclusion, n)

    cleaned = raw
    cleaned = re.sub(
        r"以下为子智能体[\s\S]{0,200}?---\s*",
        "",
        cleaned,
        count=1,
    )
    return _clip(cleaned, n)


def _parse_status_detail(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        return {"message": "", "steps": []}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            steps = parsed.get("steps")
            if not isinstance(steps, list):
                parsed["steps"] = []
            return parsed
    except json.JSONDecodeError:
        return {"message": text, "steps": []}
    return {"message": "", "steps": []}


def _dump_status_detail(detail: dict[str, Any]) -> str:
    return json.dumps(detail or {}, ensure_ascii=False, default=str)


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status or "", status or "")


def _report_to_detail(row: ResearchReport) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    raw = row.tool_calls or "[]"
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            tool_calls = parsed
    except json.JSONDecodeError:
        logger.warning("报告 tool_calls JSON 损坏 id=%s", row.id)
        tool_calls = []
    status = (row.status or STATUS_DONE) or STATUS_DONE
    detail = _parse_status_detail(row.status_detail or "")
    started_at = str(detail.get("started_at") or row.created_at or "")
    return {
        "id": int(row.id),
        "code": row.code,
        "name": row.name or "",
        "question": row.question,
        "model": row.model or "",
        "analysis": row.analysis or "",
        "tool_calls": tool_calls,
        "tool_rounds": int(row.tool_rounds or 0),
        "framework": row.framework or "",
        "status": status,
        "status_label": _status_label(status),
        "status_detail": detail,
        "status_detail_text": str(detail.get("message") or ""),
        "analysis_run_id": str(row.analysis_run_id or ""),
        "created_at": row.created_at,
        "started_at": started_at,
        "user_id": int(row.user_id) if row.user_id is not None else None,
    }


class ActiveAnalysisExistsError(Exception):
    """同一用户下，同股票已有未完成的分析任务。"""

    def __init__(self, report: dict[str, Any]):
        self.report = report
        code = report.get("code") or ""
        name = report.get("name") or code
        rid = report.get("id")
        super().__init__(
            f"{name}（{code}）已有进行中的深研任务 #{rid}，请等待完成后再试"
        )


def _owner_filters(user_id: int | None):
    """开启认证时按 user_id 隔离；关闭时不过滤（兼容全局）。"""
    if user_id is None:
        return []
    return [ResearchReport.user_id == int(user_id)]


async def get_active_report(
    code: str, *, user_id: int | None = None
) -> dict[str, Any] | None:
    """查找该股票未完成的分析（pending / running）。

    user_id 有值时仅在该用户范围内互斥；为 None（未开认证）时保持全局互斥。
    """
    await init_db()
    code_n = (code or "").strip()
    if not code_n:
        return None
    async with async_session_scope() as session:
        row = (await session.scalars(
            select(ResearchReport)
            .where(
                ResearchReport.code == code_n,
                ResearchReport.status.in_([STATUS_PENDING, STATUS_RUNNING]),
                *_owner_filters(user_id),
            )
            .order_by(ResearchReport.created_at.desc(), ResearchReport.id.desc())
            .limit(1)
        )).first()
        if not row:
            return None
        detail = _parse_status_detail(row.status_detail or "")
        status = (row.status or STATUS_RUNNING) or STATUS_RUNNING
        return {
            "id": int(row.id),
            "code": row.code,
            "name": row.name or "",
            "model": row.model or "",
            "created_at": row.created_at,
            "started_at": str(detail.get("started_at") or row.created_at or ""),
            "status": status,
            "status_label": _status_label(status),
            "status_detail": detail,
            "status_detail_text": str(detail.get("message") or ""),
            "user_id": int(row.user_id) if row.user_id is not None else None,
        }


RESTARTABLE_STATUSES = {STATUS_CANCELLED, STATUS_ERROR}


class ReportRestartError(Exception):
    """无法在原任务上重跑。"""

    def __init__(self, message: str, report: dict[str, Any] | None = None):
        self.report = report
        super().__init__(message)


async def get_analysis_run_id(report_id: int) -> str | None:
    """读取报告当前生效的分析轮次 id（落库字段）。"""
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(ResearchReport, int(report_id))
        if not row:
            return None
        aid = str(row.analysis_run_id or "").strip()
        return aid or None


async def reactivate_report(
    report_id: int,
    *,
    model: str = "",
    question: str | None = None,
    framework: str = "",
    message: str = "在原任务上重新启动分析…",
    analysis_run_id: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """将已取消/失败的任务重置为 running，复用同一条记录。"""
    await init_db()
    rid = int(report_id)
    run_id = (analysis_run_id or "").strip()
    if not run_id:
        raise ValueError("analysis_run_id 不能为空")
    async with async_session_scope() as session:
        row = await session.get(ResearchReport, rid)
        if not row:
            raise ReportRestartError("报告不存在")
        if user_id is not None and (
            row.user_id is None or int(row.user_id) != int(user_id)
        ):
            raise ReportRestartError("报告不存在")
        status = str(row.status or "")
        if status not in RESTARTABLE_STATUSES:
            raise ReportRestartError(
                f"当前状态为「{_status_label(status)}」，无法重跑",
                report=_report_to_detail(row),
            )
        code_n = row.code
        owner_id = int(user_id) if user_id is not None else (
            int(row.user_id) if row.user_id is not None else None
        )
        active = (await session.scalars(
            select(ResearchReport)
            .where(
                ResearchReport.code == code_n,
                ResearchReport.status.in_([STATUS_PENDING, STATUS_RUNNING]),
                ResearchReport.id != rid,
                *_owner_filters(owner_id),
            )
            .order_by(ResearchReport.created_at.desc(), ResearchReport.id.desc())
            .limit(1)
        )).first()
        if active:
            raise ActiveAnalysisExistsError(
                {
                    "id": int(active.id),
                    "code": active.code,
                    "name": active.name or "",
                    "status": active.status,
                    "user_id": int(active.user_id) if active.user_id is not None else None,
                }
            )

        ts = _now_iso()
        detail = {
            "message": message,
            "stage": "",
            "stage_index": 0,
            "stage_total": 6,
            "stage_title": "",
            "stage_status": "",
            "started_at": ts,
            "updated_at": ts,
            "analysis_run_id": run_id,
            "steps": [{"ts": ts, "kind": "status", "message": message}],
        }
        if question is not None:
            row.question = question
        if model:
            row.model = model
        if framework:
            row.framework = framework
        row.analysis = ""
        row.tool_calls = "[]"
        row.tool_rounds = 0
        row.created_at = ts
        row.status = STATUS_RUNNING
        row.status_detail = _dump_status_detail(detail)
        row.analysis_run_id = run_id
    logger.info(
        "reactivated research report id=%s code=%s run=%s",
        rid,
        code_n,
        run_id,
    )
    item = await get_report(rid, user_id=user_id)
    if not item:
        raise ReportRestartError("重跑后读取报告失败")
    return item


async def create_pending_report(
    *,
    code: str,
    name: str | None = None,
    question: str | None = None,
    model: str = "",
    framework: str = "",
    message: str = "已创建分析任务，等待启动…",
    analysis_run_id: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """开始分析时立刻落库一条 running 记录。"""
    await init_db()
    code_n = (code or "").strip()
    if not code_n:
        raise ValueError("code 不能为空")
    run_id = (analysis_run_id or "").strip()
    if not run_id:
        raise ValueError("analysis_run_id 不能为空")

    active = await get_active_report(code_n, user_id=user_id)
    if active:
        raise ActiveAnalysisExistsError(active)

    created_at = _now_iso()
    detail = {
        "message": message,
        "stage": "",
        "stage_index": 0,
        "stage_total": 6,
        "stage_title": "",
        "stage_status": "",
        "started_at": created_at,
        "updated_at": created_at,
        "analysis_run_id": run_id,
        "steps": [
            {
                "ts": created_at,
                "kind": "status",
                "message": message,
            }
        ],
    }
    async with async_session_scope() as session:
        row = ResearchReport(
            code=code_n,
            name=(name or "").strip(),
            question=question,
            model=model or "",
            analysis="",
            tool_calls="[]",
            tool_rounds=0,
            framework=framework or "",
            created_at=created_at,
            status=STATUS_RUNNING,
            status_detail=_dump_status_detail(detail),
            analysis_run_id=run_id,
            user_id=int(user_id) if user_id is not None else None,
        )
        session.add(row)
        await session.flush()
        report_id = int(row.id)
    logger.info(
        "created pending research report id=%s code=%s run=%s user=%s",
        report_id,
        code_n,
        run_id,
        user_id,
    )
    return {
        "id": report_id,
        "code": code_n,
        "name": (name or "").strip(),
        "created_at": created_at,
        "started_at": created_at,
        "status": STATUS_RUNNING,
        "status_detail": detail,
        "analysis_run_id": run_id,
        "user_id": int(user_id) if user_id is not None else None,
    }


async def update_report_progress(
    report_id: int,
    *,
    status: str | None = None,
    message: str | None = None,
    stage: dict[str, Any] | None = None,
    kind: str = "status",
) -> dict[str, Any] | None:
    """追加进度步骤并更新 status / status_detail。"""
    await init_db()
    rid = int(report_id)
    async with async_session_scope() as session:
        row = await session.get(ResearchReport, rid)
        if not row:
            return None

        cur_status = str(row.status or "")
        if cur_status == STATUS_CANCELLED and status not in {STATUS_CANCELLED, None}:
            if status != STATUS_CANCELLED:
                return {
                    "id": rid,
                    "status": STATUS_CANCELLED,
                    "status_detail": _parse_status_detail(row.status_detail or ""),
                }
        if (
            cur_status == STATUS_CANCELLED
            and status is None
            and kind in {"stage", "tool", "status"}
        ):
            return {
                "id": rid,
                "status": STATUS_CANCELLED,
                "status_detail": _parse_status_detail(row.status_detail or ""),
            }

        detail = _parse_status_detail(row.status_detail or "")
        steps = list(detail.get("steps") or [])
        ts = _now_iso()
        msg = (message or "").strip()
        if stage:
            detail["stage"] = stage.get("stage") or detail.get("stage") or ""
            detail["stage_index"] = int(
                stage.get("index") or detail.get("stage_index") or 0
            )
            detail["stage_total"] = int(
                stage.get("total") or detail.get("stage_total") or 6
            )
            detail["stage_title"] = stage.get("title") or detail.get("stage_title") or ""
            detail["stage_status"] = (
                stage.get("status") or detail.get("stage_status") or ""
            )
            if not msg:
                msg = str(stage.get("message") or "")
        if msg:
            detail["message"] = msg
            entry: dict[str, Any] = {
                "ts": ts,
                "kind": kind or "status",
                "message": msg,
            }
            if stage:
                entry["stage"] = stage.get("stage") or ""
                entry["stage_status"] = stage.get("status") or ""
                entry["index"] = stage.get("index")
                entry["total"] = stage.get("total")
                entry["title"] = stage.get("title") or ""
            steps.append(entry)
            detail["steps"] = steps[-_MAX_STEPS:]
        detail["updated_at"] = ts

        next_status = status or row.status or STATUS_RUNNING
        row.status = next_status
        row.status_detail = _dump_status_detail(detail)
    return {
        "id": rid,
        "status": next_status,
        "status_detail": detail,
    }


async def finalize_report(
    report_id: int,
    *,
    analysis: str,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_rounds: int = 0,
    framework: str = "",
    model: str = "",
    name: str | None = None,
    question: str | None = None,
    message: str = "分析完成",
) -> dict[str, Any] | None:
    await init_db()
    rid = int(report_id)
    analysis_n = (analysis or "").strip()
    if not analysis_n:
        raise ValueError("analysis 不能为空")

    ts = _now_iso()
    async with async_session_scope() as session:
        row = await session.get(ResearchReport, rid)
        if not row:
            return None

        cur_status = str(row.status or "")
        if cur_status in {STATUS_CANCELLED, STATUS_ERROR}:
            logger.info(
                "skip finalize report id=%s because status=%s", rid, cur_status
            )
            return None

        detail = _parse_status_detail(row.status_detail or "")
        steps = list(detail.get("steps") or [])
        detail.update(
            {
                "message": message,
                "stage": "stage_6_report",
                "stage_index": 6,
                "stage_total": 6,
                "stage_title": "综合报告",
                "stage_status": "end",
                "updated_at": ts,
            }
        )
        steps.append({"ts": ts, "kind": "status", "message": message})
        detail["steps"] = steps[-_MAX_STEPS:]

        payload = json.dumps(tool_calls or [], ensure_ascii=False, default=str)
        name_n = (name or "").strip()
        if name_n:
            row.name = name_n
        if question is not None:
            row.question = question
        if model:
            row.model = model
        if framework:
            row.framework = framework
        row.analysis = analysis_n
        row.tool_calls = payload
        row.tool_rounds = int(tool_rounds or 0)
        row.status = STATUS_DONE
        row.status_detail = _dump_status_detail(detail)
    logger.info("finalized research report id=%s", rid)
    return await get_report(rid)


async def mark_report_error(report_id: int, message: str) -> dict[str, Any] | None:
    return await update_report_progress(
        report_id,
        status=STATUS_ERROR,
        message=message or "分析失败",
        kind="error",
    )


async def fail_orphan_running_reports(
    message: str = "服务已重启，进行中的分析已中断（可重跑）",
) -> list[int]:
    """进程启动时回收无主的 pending/running（内存任务表已空）。"""
    await init_db()
    msg = message or "服务已重启，进行中的分析已中断（可重跑）"
    async with async_session_scope() as session:
        rows = (await session.scalars(
            select(ResearchReport.id)
            .where(ResearchReport.status.in_([STATUS_PENDING, STATUS_RUNNING]))
            .order_by(ResearchReport.id)
        )).all()
        touched = [int(rid) for rid in rows]
    for rid in touched:
        await mark_report_error(rid, msg)
    if touched:
        logger.warning(
            "recovered orphan running reports: %s (%s)",
            touched,
            msg,
        )
    return touched


async def mark_report_cancelled(
    report_id: int,
    message: str = "用户取消了分析",
) -> dict[str, Any] | None:
    return await update_report_progress(
        report_id,
        status=STATUS_CANCELLED,
        message=message or "用户取消了分析",
        kind="status",
    )


async def get_report_status(report_id: int) -> str | None:
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(ResearchReport, int(report_id))
        if not row:
            return None
        return str(row.status or "")


async def save_report(
    *,
    code: str,
    name: str | None = None,
    question: str | None = None,
    model: str = "",
    analysis: str,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_rounds: int = 0,
    framework: str = "",
    user_id: int | None = None,
) -> dict[str, Any]:
    """直接保存一条已完成报告（兼容旧接口）。"""
    await init_db()
    code_n = (code or "").strip()
    analysis_n = (analysis or "").strip()
    if not code_n or not analysis_n:
        raise ValueError("code 与 analysis 不能为空")

    created_at = _now_iso()
    run_id = new_run_id()
    detail = {
        "message": "分析完成",
        "stage": "stage_6_report",
        "stage_index": 6,
        "stage_total": 6,
        "stage_title": "综合报告",
        "stage_status": "end",
        "updated_at": created_at,
        "analysis_run_id": run_id,
        "steps": [{"ts": created_at, "kind": "status", "message": "分析完成"}],
    }
    payload = json.dumps(tool_calls or [], ensure_ascii=False, default=str)
    async with async_session_scope() as session:
        row = ResearchReport(
            code=code_n,
            name=(name or "").strip(),
            question=question,
            model=model or "",
            analysis=analysis_n,
            tool_calls=payload,
            tool_rounds=int(tool_rounds or 0),
            framework=framework or "",
            created_at=created_at,
            status=STATUS_DONE,
            status_detail=_dump_status_detail(detail),
            analysis_run_id=run_id,
            user_id=int(user_id) if user_id is not None else None,
        )
        session.add(row)
        await session.flush()
        report_id = int(row.id)
    logger.info("saved research report id=%s code=%s user=%s", report_id, code_n, user_id)
    return {
        "id": report_id,
        "code": code_n,
        "name": (name or "").strip(),
        "created_at": created_at,
        "status": STATUS_DONE,
        "user_id": int(user_id) if user_id is not None else None,
    }


async def list_reports(
    *,
    q: str | None = None,
    code: str | None = None,
    limit: int = 30,
    offset: int = 0,
    user_id: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    await init_db()
    limit = max(1, min(int(limit or 30), 100))
    offset = max(0, int(offset or 0))

    filters = list(_owner_filters(user_id))
    code_n = (code or "").strip()
    if code_n:
        filters.append(ResearchReport.code == code_n)

    q_n = (q or "").strip()
    if q_n:
        like = f"%{q_n}%"
        filters.append(
            or_(
                ResearchReport.code.like(like),
                ResearchReport.name.like(like),
                ResearchReport.analysis.like(like),
                ResearchReport.status_detail.like(like),
            )
        )

    async with async_session_scope() as session:
        count_stmt = select(func.count()).select_from(ResearchReport)
        list_stmt = select(ResearchReport)
        if filters:
            count_stmt = count_stmt.where(*filters)
            list_stmt = list_stmt.where(*filters)
        total = int(await session.scalar(count_stmt) or 0)
        rows = (await session.scalars(
            list_stmt.order_by(
                ResearchReport.created_at.desc(),
                ResearchReport.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )).all()
        # detach field snapshots before session closes
        snapshots = [
            {
                "id": int(r.id),
                "code": r.code,
                "name": r.name or "",
                "model": r.model or "",
                "tool_rounds": int(r.tool_rounds or 0),
                "created_at": r.created_at,
                "analysis": r.analysis or "",
                "status": (r.status or STATUS_DONE) or STATUS_DONE,
                "status_detail": r.status_detail or "",
                "analysis_run_id": str(r.analysis_run_id or ""),
                "user_id": int(r.user_id) if r.user_id is not None else None,
            }
            for r in rows
        ]

    ids = [s["id"] for s in snapshots]
    counts = count_messages_by_report_ids(ids)
    items = []
    for s in snapshots:
        detail = _parse_status_detail(s["status_detail"])
        preview = _preview(s["analysis"])
        if not preview and detail.get("message"):
            preview = _preview(str(detail.get("message") or ""))
        items.append(
            {
                "id": s["id"],
                "code": s["code"],
                "name": s["name"],
                "model": s["model"],
                "tool_rounds": s["tool_rounds"],
                "created_at": s["created_at"],
                "started_at": str(detail.get("started_at") or s["created_at"] or ""),
                "preview": preview,
                "message_count": int(counts.get(s["id"], 0)),
                "status": s["status"],
                "status_label": _status_label(s["status"]),
                "status_detail": detail,
                "status_detail_text": str(detail.get("message") or ""),
                "analysis_run_id": s["analysis_run_id"],
                "user_id": s.get("user_id"),
            }
        )
    return total, items


async def get_report(
    report_id: int, *, user_id: int | None = None
) -> dict[str, Any] | None:
    await init_db()
    async with async_session_scope() as session:
        row = await session.get(ResearchReport, int(report_id))
        if not row:
            return None
        if user_id is not None and (
            row.user_id is None or int(row.user_id) != int(user_id)
        ):
            return None
        detail = _report_to_detail(row)
    detail["message_count"] = count_messages(int(report_id))
    return detail


async def delete_report(
    report_id: int, *, user_id: int | None = None
) -> bool:
    await init_db()
    rid = int(report_id)
    async with async_session_scope() as session:
        # 旧版 research_messages 仍可能有行 + FK，不删会拦 research_reports DELETE
        def _has_messages(sync_session) -> bool:
            bind = sync_session.get_bind()
            return bool(bind is not None and inspect(bind).has_table("research_messages"))

        if await session.run_sync(_has_messages):
            await session.execute(
                text("DELETE FROM research_messages WHERE report_id = :rid"),
                {"rid": rid},
            )
        row = await session.get(ResearchReport, rid)
        if not row:
            ok = False
        elif user_id is not None and (
            row.user_id is None or int(row.user_id) != int(user_id)
        ):
            ok = False
        else:
            await session.delete(row)
            ok = True
    if ok:
        # 报告行删除成功后再清 DocStore 对话（避免 404 误删聊天）
        delete_messages_for_report(rid)
        logger.info("已删除深研报告 #%s（业务库记录与对话）", rid)
    else:
        logger.info("删除深研报告未命中 #%s", rid)
    return ok
