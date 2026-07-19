"""agents 表 CRUD (SPEC §4-1)。"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent


class AgentService:
    """纯函数式 CRUD。Repository 风格，便于在 FastAPI Depends 之外复用。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, record: Agent) -> Agent:
        """注册或覆盖：以 agent_id 为主键冲突即更新。"""
        await self.session.merge(record)
        await self.session.flush()
        return record

    async def get(self, agent_id: str) -> Agent | None:
        return await self.session.get(Agent, agent_id)

    async def list_active(self) -> Sequence[Agent]:
        stmt = select(Agent).where(Agent.status == "active")
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def list_all(self) -> Sequence[Agent]:
        stmt = select(Agent)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def update_status(self, agent_id: str, status: str) -> None:
        agent = await self.session.get(Agent, agent_id)
        if agent is None:
            return
        agent.status = status
        await self.session.flush()

    async def reset_health_failures(self, agent_id: str) -> None:
        agent = await self.session.get(Agent, agent_id)
        if agent is None:
            return
        agent.consecutive_health_failures = 0
        await self.session.flush()

    async def increment_health_failures(self, agent_id: str) -> int:
        agent = await self.session.get(Agent, agent_id)
        if agent is None:
            return 0
        agent.consecutive_health_failures = (agent.consecutive_health_failures or 0) + 1
        await self.session.flush()
        return agent.consecutive_health_failures

    async def mark_requested_now(self, agent_id: str, ts: datetime | None = None) -> None:
        agent = await self.session.get(Agent, agent_id)
        if agent is None:
            return
        agent.last_request_at = ts or datetime.utcnow()
        await self.session.flush()
