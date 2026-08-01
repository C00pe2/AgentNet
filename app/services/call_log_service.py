"""agent_call_logs 写入与查询 (SPEC §4-4 + §7-Task 4)。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AgentCallLog

logger = logging.getLogger(__name__)


class CallLogService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        *,
        plan_id: int | None,
        session_id: str,
        target_agent_id: str,
        step_id: int,
        request_snapshot: dict[str, Any],
        response_snapshot: dict[str, Any] | None,
        code: int,
        latency_ms: int,
        status: str,
    ) -> AgentCallLog:
        log = AgentCallLog(
            plan_id=plan_id,
            session_id=session_id,
            target_agent_id=target_agent_id,
            step_id=step_id,
            request_snapshot=request_snapshot,
            response_snapshot=response_snapshot,
            code=code,
            latency_ms=latency_ms,
            status=status,
        )
        self.session.add(log)
        await self.session.flush()
        await self.session.refresh(log)
        return log


async def fire_and_forget_record(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    plan_id: int | None,
    session_id: str,
    target_agent_id: str,
    step_id: int,
    request_snapshot: dict[str, Any],
    response_snapshot: dict[str, Any] | None,
    code: int,
    latency_ms: int,
    status: str,
) -> None:
    """将审计写入任务丢到后台，不阻塞主流程。

    SPEC §7-Task 4 要求 "编写 agent_call_logs 异步写入"。
    """
    async def _job() -> None:
        try:
            async with sessionmaker() as session:
                async with session.begin():
                    svc = CallLogService(session)
                    await svc.record(
                        plan_id=plan_id,
                        session_id=session_id,
                        target_agent_id=target_agent_id,
                        step_id=step_id,
                        request_snapshot=request_snapshot,
                        response_snapshot=response_snapshot,
                        code=code,
                        latency_ms=latency_ms,
                        status=status,
                    )
        except Exception:  # noqa: BLE001 - 审计失败也不影响主流程
            logger.exception(
                "agent_call_logs 异步写入失败 plan_id=%s target=%s",
                plan_id,
                target_agent_id,
            )

    asyncio.create_task(_job())
