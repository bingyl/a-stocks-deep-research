from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.agent import analyze_fundamentals
from app.agent.analyzer import stream_analyze_fundamentals
from app.agent.sse_bridge import iter_detached
from app.core.logging import log_caught
from app.deps.auth import CurrentUser, get_current_user, scoped_user_id
from app.models.agent import AgentAnalyzeResponse, AnalyzeRequest
from app.services import reports as reports_svc
from app.services.reports import (
    ActiveAnalysisExistsError,
    RESTARTABLE_STATUSES,
    ReportRestartError,
)
from app.services.stock import StockNotFoundError, normalize_code

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _active_conflict_detail(active: dict[str, Any]) -> dict[str, Any]:
    code = active.get("code") or ""
    name = active.get("name") or code
    rid = active.get("id")
    return {
        "message": f"{name}（{code}）已有进行中的深研任务 #{rid}，请等待完成后再试",
        "report_id": rid,
        "code": code,
        "name": name,
        "status": active.get("status") or "running",
        "created_at": active.get("created_at") or "",
        "status_detail_text": active.get("status_detail_text") or "",
    }


@router.post(
    "/analyze",
    response_model=AgentAnalyzeResponse,
    summary="AI 基本面分析（一次性返回）",
)
async def analyze(
    req: AnalyzeRequest,
    user: CurrentUser | None = Depends(get_current_user),
):
    try:
        uid = scoped_user_id(user)
        logger.info("POST /analyze code=%s user=%s", req.code, uid)
        code = normalize_code(req.code)
        active = await reports_svc.get_active_report(code, user_id=uid)
        if active:
            raise HTTPException(status_code=409, detail=_active_conflict_detail(active))
        result = await analyze_fundamentals(req.code, req.question)
        return AgentAnalyzeResponse(**result)
    except HTTPException:
        raise
    except ActiveAnalysisExistsError as exc:
        raise HTTPException(
            status_code=409, detail=_active_conflict_detail(exc.report)
        ) from exc
    except StockNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        log_caught(logger, "analyze failed", exc=exc, level=logging.ERROR)
        raise HTTPException(status_code=502, detail=f"智能体分析失败: {exc}") from exc


@router.post(
    "/analyze/stream",
    summary="AI 基本面分析（SSE 流式进度）",
)
async def analyze_stream(
    req: AnalyzeRequest,
    user: CurrentUser | None = Depends(get_current_user),
):
    uid = scoped_user_id(user)
    logger.info(
        "POST /analyze/stream code=%s report_id=%s user=%s",
        req.code,
        req.report_id,
        uid,
    )
    code = normalize_code(req.code)
    reuse_id: int | None = int(req.report_id) if req.report_id else None

    if reuse_id:
        existing = await reports_svc.get_report(reuse_id, user_id=uid)
        if not existing:
            raise HTTPException(status_code=404, detail="要重跑的报告不存在")
        if normalize_code(str(existing.get("code") or "")) != code:
            raise HTTPException(status_code=400, detail="报告股票代码与请求不一致")
        st = existing.get("status") or ""
        if st not in RESTARTABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"当前状态为「{existing.get('status_label') or st}」，无法在原任务上重跑",
            )
        active = await reports_svc.get_active_report(code, user_id=uid)
        if active and int(active["id"]) != reuse_id:
            raise HTTPException(status_code=409, detail=_active_conflict_detail(active))
    else:
        active = await reports_svc.get_active_report(code, user_id=uid)
        if active:
            raise HTTPException(status_code=409, detail=_active_conflict_detail(active))

    async def event_gen() -> AsyncIterator[str]:
        # 分析在独立 Task 中跑：客户端断开/前端切到另一只股票 abort 时，不取消分析
        try:
            async for item in iter_detached(
                stream_analyze_fundamentals(
                    req.code,
                    req.question,
                    report_id=reuse_id,
                    user_id=uid,
                ),
                label=code,
            ):
                yield _sse(item["event"], item.get("data") or {})
        except asyncio.CancelledError:
            raise
        except ActiveAnalysisExistsError as exc:
            yield _sse("active_analysis_exists", _active_conflict_detail(exc.report))
        except ReportRestartError as exc:
            yield _sse("error", {"message": str(exc)})
        except Exception as exc:
            log_caught(logger, "analyze stream failed", exc=exc, level=logging.ERROR)
            yield _sse("error", {"message": f"智能体分析失败: {exc}"})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
