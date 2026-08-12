from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.logging import log_caught
from app.persistence.db.async_runner import run_coro
from app.services.sync_stocks import sync_stock_universe
from app.services.universe import reload_from_db

logger = logging.getLogger(__name__)

_scheduler: Optional[BackgroundScheduler] = None


async def _async_daily_sync() -> None:
    result = await sync_stock_universe(full=False, refresh_industry=True)
    await reload_from_db()
    logger.info("每日同步完成: %s", result)


def _run_daily_sync() -> None:
    logger.info("开始每日股票池同步")
    try:
        run_coro(_async_daily_sync())
    except Exception as exc:
        log_caught(logger, "每日股票池同步失败", exc=exc, level=logging.ERROR)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        _run_daily_sync,
        CronTrigger(hour=8, minute=30, timezone="Asia/Shanghai"),
        id="daily_stock_universe_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("已启动股票池定时任务：每天 08:30（Asia/Shanghai）")
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
