"""信誉聚合:从 Gateway 调用记录实时计算(不存储,保证永远一致)。"""

from __future__ import annotations

from agentnet_core import Reputation
from agentnet_core.enums import TaskState
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import CallLogRow


async def load_reputations(session: AsyncSession, agent_ids: list[str]) -> dict[str, Reputation]:
    if not agent_ids:
        return {}
    stmt = (
        select(
            CallLogRow.agent_id,
            func.count().label("calls"),
            func.avg(
                case((CallLogRow.final_state == TaskState.COMPLETED.value, 1.0), else_=0.0)
            ).label("success_rate"),
            func.avg(CallLogRow.latency_ms).label("avg_latency_ms"),
            func.avg(CallLogRow.rating).label("rating"),
        )
        .where(CallLogRow.agent_id.in_(agent_ids), CallLogRow.final_state.is_not(None))
        .group_by(CallLogRow.agent_id)
    )
    result: dict[str, Reputation] = {}
    for row in (await session.execute(stmt)).all():
        result[row.agent_id] = Reputation(
            calls=row.calls,
            success_rate=float(row.success_rate or 0.0),
            avg_latency_ms=float(row.avg_latency_ms or 0.0),
            rating=float(row.rating) if row.rating is not None else None,
        )
    return result
