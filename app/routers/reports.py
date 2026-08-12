from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.agent.followup import stream_followup_chat
from app.core.logging import log_caught
from app.deps.auth import CurrentUser, get_current_user, scoped_user_id
from app.persistence.checkpointer import delete_checkpoints_for_report
from app.rag.ingest import purge_report_rag
from app.models.reports import (
    ResearchChatRequest,
    ResearchMessageItem,
    ResearchMessageListResponse,
    ResearchReportCreate,
    ResearchReportDetail,
    ResearchReportListResponse,
    ResearchReportSaveResponse,
    ResearchReportSummary,
)
from app.services import analysis_jobs
from app.services import chat as chat_svc
from app.services import reports as reports_svc
from app.services.compare import build_compare_payload
from app.services.reports import STATUS_CANCELLED, STATUS_PENDING, STATUS_RUNNING

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reports", tags=["reports"])


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post(
    "",
    response_model=ResearchReportSaveResponse,
    summary="保存一条深研报告",
)
async def create_report(
    req: ResearchReportCreate,
    user: CurrentUser | None = Depends(get_current_user),
):
    try:
        uid = scoped_user_id(user)
        saved = await reports_svc.save_report(
            code=req.code,
            name=req.name,
            question=req.question,
            model=req.model,
            analysis=req.analysis,
            tool_calls=req.tool_calls,
            tool_rounds=req.tool_rounds,
            framework=req.framework,
            user_id=uid,
        )
        return ResearchReportSaveResponse(**saved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "save report failed", exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=500, detail=f"保存失败: {exc}") from exc


@router.get(
    "",
    response_model=ResearchReportListResponse,
    summary="深研历史列表",
)
async def list_reports(
    q: str | None = Query(None, max_length=64, description="关键词：代码 / 名称 / 正文"),
    code: str | None = Query(None, max_length=16, description="精确股票代码"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    total, items = await reports_svc.list_reports(
        q=q, code=code, limit=limit, offset=offset, user_id=uid
    )
    return ResearchReportListResponse(
        total=total,
        items=[ResearchReportSummary(**x) for x in items],
    )


@router.get(
    "/compare",
    summary="对比两份深研报告（同股或同业）",
)
async def compare_reports(
    ids: str = Query(..., description="两个报告 id，逗号分隔，如 12,15"),
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    parts = [p.strip() for p in (ids or "").split(",") if p.strip()]
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="请提供恰好两个报告 id，如 ids=12,15")
    try:
        id_a, id_b = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="报告 id 无效") from exc
    if id_a == id_b:
        raise HTTPException(status_code=400, detail="请选择两份不同的报告")

    ra = await reports_svc.get_report(id_a, user_id=uid)
    rb = await reports_svc.get_report(id_b, user_id=uid)
    if not ra or not rb:
        raise HTTPException(status_code=404, detail="报告不存在")
    if (ra.get("status") or "") != "done" or (rb.get("status") or "") != "done":
        raise HTTPException(status_code=400, detail="只能对比已完成的报告")
    if not (ra.get("analysis") or "").strip() or not (rb.get("analysis") or "").strip():
        raise HTTPException(status_code=400, detail="报告正文为空，无法对比")

    return build_compare_payload([ra, rb])


@router.post(
    "/{report_id}/cancel",
    summary="取消进行中的深研任务",
)
async def cancel_report(
    report_id: int,
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    item = await reports_svc.get_report(report_id, user_id=uid)
    if not item:
        raise HTTPException(status_code=404, detail="报告不存在")
    status = item.get("status") or ""
    if status not in {STATUS_PENDING, STATUS_RUNNING}:
        raise HTTPException(
            status_code=400,
            detail=f"当前状态为「{item.get('status_label') or status}」，无法取消",
        )
    run_id = await analysis_jobs.current_run_id(report_id)
    cancelled_run = await analysis_jobs.request_cancel(report_id)
    updated = await reports_svc.mark_report_cancelled(report_id)
    return {
        "ok": True,
        "id": report_id,
        "analysis_run_id": run_id,
        "drain_requested": cancelled_run,
        "status": (updated or {}).get("status") or STATUS_CANCELLED,
        "message": "已取消分析",
    }


@router.get(
    "/{report_id}",
    response_model=ResearchReportDetail,
    summary="深研报告详情",
)
async def get_report(
    report_id: int,
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    item = await reports_svc.get_report(report_id, user_id=uid)
    if not item:
        raise HTTPException(status_code=404, detail="报告不存在")
    return ResearchReportDetail(**item)


@router.delete(
    "/{report_id}",
    summary="删除深研报告",
)
async def delete_report(
    report_id: int,
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    # 必须先 cancel/drain，再删库：否则后台分析 Task 会继续跑（烧 token）
    existing = await reports_svc.get_report(report_id, user_id=uid)
    if not existing:
        raise HTTPException(status_code=404, detail="报告不存在")
    run_id = str(existing.get("analysis_run_id") or "").strip() or None
    status = str(existing.get("status") or "")
    drained = False
    if status in {STATUS_PENDING, STATUS_RUNNING} or run_id:
        if run_id:
            drained = analysis_jobs.request_cancel_run(run_id)
        else:
            drained = await analysis_jobs.request_cancel(report_id)
        logger.info(
            "删除前请求取消分析 id=%s status=%s run=%s drain=%s",
            report_id,
            status,
            run_id or "-",
            drained,
        )

    ok = await reports_svc.delete_report(report_id, user_id=uid)
    if not ok:
        raise HTTPException(status_code=404, detail="报告不存在")
    try:
        purge_report_rag(str(int(report_id)))
        logger.info("删除报告后知识库清理完成 id=%s", report_id)
    except Exception as exc:
        log_caught(logger, "删除报告后清理知识库失败 id=%s", report_id, exc=exc, level=logging.ERROR)
    try:
        n = await delete_checkpoints_for_report(int(report_id))
        logger.info("删除报告后 checkpoint 清理完成 id=%s threads=%s", report_id, n)
    except Exception as exc:
        log_caught(
            logger,
            "删除报告后清理 checkpoint 失败 id=%s",
            report_id,
            exc=exc,
            level=logging.ERROR,
        )
    return {
        "ok": True,
        "id": report_id,
        "analysis_run_id": run_id,
        "drain_requested": drained,
    }


@router.get(
    "/{report_id}/messages",
    response_model=ResearchMessageListResponse,
    summary="深研追问对话列表",
)
async def list_report_messages(
    report_id: int,
    limit: int = Query(200, ge=1, le=500),
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    report = await reports_svc.get_report(report_id, user_id=uid)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    items = chat_svc.list_messages(report_id, limit=limit)
    return ResearchMessageListResponse(
        report_id=report_id,
        total=len(items),
        items=[ResearchMessageItem(**x) for x in items],
    )


@router.delete(
    "/{report_id}/messages",
    summary="清空该报告的追问记录",
)
async def clear_report_messages(
    report_id: int,
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    report = await reports_svc.get_report(report_id, user_id=uid)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    n = chat_svc.delete_messages(report_id)
    return {"ok": True, "deleted": n}


@router.post(
    "/{report_id}/chat/stream",
    summary="基于深研报告继续追问（SSE）",
)
async def chat_stream(
    report_id: int,
    req: ResearchChatRequest,
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    report = await reports_svc.get_report(report_id, user_id=uid)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")

    async def event_gen() -> AsyncIterator[str]:
        try:
            async for item in stream_followup_chat(report_id, req.message):
                yield _sse(item["event"], item.get("data") or {})
        except Exception as exc:
            log_caught(logger, "chat stream failed", exc=exc, level=logging.ERROR)
            yield _sse("error", {"message": f"追问失败: {exc}"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
