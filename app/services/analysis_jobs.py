from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from langgraph.runtime import RunControl

from app.core.logging import reset_log_run_id, set_log_run_id
from app.services.reports import get_analysis_run_id
from app.services.run_ids import new_run_id

logger = logging.getLogger(__name__)

__all__ = [
    "AnalysisCancelled",
    "active_job_ids",
    "active_run_ids",
    "cancel_payload",
    "current_run_id",
    "get_control",
    "get_job",
    "is_cancel_requested",
    "is_current_run",
    "is_superseded",
    "new_run_id",
    "owns_report",
    "register",
    "request_cancel",
    "request_cancel_run",
    "unregister",
]


@dataclass
class _ActiveJob:
    run_id: str
    report_id: int
    event: asyncio.Event
    control: RunControl
    cancel_requested: bool = False


# 仅缓存「进行中」协程的 RunControl；当前轮次以 DB research_reports.analysis_run_id 为准
_jobs: dict[str, _ActiveJob] = {}


def register(report_id: int, *, run_id: str) -> RunControl:
    """为本轮分析挂上内存中的 RunControl（run_id 须已落库）。"""
    rid = int(report_id)
    aid = (run_id or "").strip()
    if not aid:
        raise ValueError("analysis_run_id 不能为空")
    control = RunControl()
    _jobs[aid] = _ActiveJob(
        run_id=aid,
        report_id=rid,
        event=asyncio.Event(),
        control=control,
    )
    logger.info("bound in-memory RunControl for report_id=%s", rid)
    return control


def unregister(run_id: str | None, report_id: int | None = None) -> None:
    """分析协程结束：只卸内存控制，不改 DB 上的 analysis_run_id。"""
    del report_id  # 保留签名兼容调用方
    if not run_id:
        return
    _jobs.pop(str(run_id), None)


def get_job(run_id: str | None) -> _ActiveJob | None:
    if not run_id:
        return None
    return _jobs.get(str(run_id))


def get_control(run_id: str | None) -> RunControl | None:
    job = get_job(run_id)
    return job.control if job else None


async def current_run_id(report_id: int | None) -> str | None:
    """当前生效轮次：读库。"""
    if report_id is None:
        return None
    return await get_analysis_run_id(int(report_id))


async def is_current_run(report_id: int | None, run_id: str | None) -> bool:
    if report_id is None or not run_id:
        return False
    return (await current_run_id(report_id)) == str(run_id)


async def owns_report(report_id: int | None, run_id: str | None) -> bool:
    """本轮是否仍是该报告的当前分析（可安全写库/清库）。以 DB 为准。"""
    return await is_current_run(report_id, run_id)


async def is_superseded(report_id: int | None, run_id: str | None) -> bool:
    """报告上已有更新的分析轮次（旧轮次迟到入库/清理应跳过）。"""
    if report_id is None or not run_id:
        return False
    current = await current_run_id(report_id)
    if not current:
        return False
    return current != str(run_id)


def is_cancel_requested(run_id: str | None) -> bool:
    job = get_job(run_id)
    if not job:
        return False
    return job.cancel_requested or job.event.is_set() or job.control.drain_requested


async def request_cancel(report_id: int) -> bool:
    """取消该报告当前轮次；Agent 在下一 superstep 边界协作退出。"""
    rid = int(report_id)
    run_id = await current_run_id(rid)
    if not run_id:
        logger.info("cancel requested but no analysis_run_id in DB report_id=%s", rid)
        return False
    return request_cancel_run(run_id)


def request_cancel_run(run_id: str) -> bool:
    job = _jobs.get(str(run_id))
    token = set_log_run_id(run_id)
    try:
        if not job:
            logger.info("cancel requested but no in-memory job run_id=%s", run_id)
            return False
        job.cancel_requested = True
        job.event.set()
        job.control.request_drain("user_cancel")
        logger.info(
            "cancel requested for active job report_id=%s (RunControl.drain)",
            job.report_id,
        )
        return True
    finally:
        reset_log_run_id(token)


def active_job_ids() -> list[int]:
    """内存中仍挂着 RunControl 的 report_id。"""
    return sorted({j.report_id for j in _jobs.values()})


def active_run_ids() -> list[str]:
    return sorted(_jobs.keys())


class AnalysisCancelled(Exception):
    """分析被用户取消。"""

    def __init__(
        self,
        report_id: int | None = None,
        message: str = "分析已取消",
        *,
        run_id: str | None = None,
    ):
        self.report_id = report_id
        self.run_id = run_id
        super().__init__(message)


def cancel_payload(
    report_id: int | None,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "message": "分析已取消",
        "report_id": report_id,
        "status": "cancelled",
    }
    if run_id:
        payload["analysis_run_id"] = run_id
    return payload
