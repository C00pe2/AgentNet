"""后台调度器：每 1 秒拉一次 HealthService.tick_all()。"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.services.health_service import HealthService

logger = logging.getLogger(__name__)


def build_scheduler(health: HealthService) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def _tick() -> None:
        try:
            await health.tick_all()
        except Exception:  # noqa: BLE001
            logger.exception("health tick_all 异常")

    scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(seconds=1),
        id="agentnet_health_tick",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def shutdown_scheduler(scheduler: AsyncIOScheduler) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
