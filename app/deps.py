"""deps.py: FastAPI 依赖工厂。"""
from typing import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session as _get_db_session
from app.services.agent_service import AgentService
from app.services.metrics_service import MetricsService
from app.services.session_service import SessionService


async def get_db() -> AsyncIterator[AsyncSession]:
    async for s in _get_db_session():
        yield s


def get_agent_service(session: AsyncSession = Depends(get_db)) -> AgentService:
    return AgentService(session)


def get_session_service(session: AsyncSession = Depends(get_db)) -> SessionService:
    return SessionService(session)


def get_metrics_service(session: AsyncSession = Depends(get_db)) -> MetricsService:
    return MetricsService(session)
