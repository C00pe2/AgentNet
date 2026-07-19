"""plan_executions 表 CRUD (SPEC §4-3 + 4.1 状态机)。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PlanExecution, PlanStatus as DbPlanStatus


class PlanService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, plan: PlanExecution) -> PlanExecution:
        self.session.add(plan)
        await self.session.flush()
        await self.session.refresh(plan)
        return plan

    async def get(self, plan_id: int) -> PlanExecution | None:
        return await self.session.get(PlanExecution, plan_id)

    async def list_by_session(self, session_id: str) -> list[PlanExecution]:
        stmt = select(PlanExecution).where(PlanExecution.session_id == session_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def update_graph(self, plan_id: int, graph: dict[str, Any]) -> None:
        plan = await self.session.get(PlanExecution, plan_id)
        if plan is None:
            return
        plan.execution_graph = graph
        await self.session.flush()

    async def mark_succeeded(self, plan_id: int) -> None:
        await self._finish(plan_id, DbPlanStatus.SUCCEEDED)

    async def mark_failed(self, plan_id: int) -> None:
        await self._finish(plan_id, DbPlanStatus.FAILED)

    async def _finish(self, plan_id: int, status: DbPlanStatus) -> None:
        plan = await self.session.get(PlanExecution, plan_id)
        if plan is None:
            return
        plan.status = status.value
        plan.finished_at = datetime.utcnow()
        await self.session.flush()
