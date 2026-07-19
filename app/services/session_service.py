"""agent_sessions 表 CRUD (SPEC §4-2)。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentSession


class SessionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def upsert(self, record: AgentSession) -> AgentSession:
        await self.session.merge(record)
        await self.session.flush()
        return record

    async def get(self, session_id: str) -> AgentSession | None:
        return await self.session.get(AgentSession, session_id)
