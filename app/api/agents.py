"""专家 Agent 注册/查询 API。"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.db.models import Agent
from app.deps import get_agent_service
from app.schemas.agent import AgentCreate, AgentRead
from app.schemas.response import Envelope, success_envelope
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("", response_model=Envelope[AgentRead])
async def register_agent(
    payload: AgentCreate,
    svc: AgentService = Depends(get_agent_service),
) -> Envelope[AgentRead]:
    """注册或覆盖一个专家 Agent (以 agent_id 为主键)。

    MVP 阶段 auth_token 暂以明文存储，v1.1 接入 KMS / Vault。
    """
    record = Agent(
        agent_id=payload.agent_id,
        name=payload.name,
        owner_dept=payload.owner_department,
        description=payload.description,
        endpoint_url=str(payload.endpoint.url),
        auth_token=payload.endpoint.x_agentnet_token,
        timeout_ms=payload.sla.timeout_ms,
        max_retry=payload.sla.max_retry,
        status="active",
        consecutive_health_failures=0,
    )
    await svc.upsert(record)
    await svc.session.commit()

    return success_envelope(_to_read(record))


@router.get("", response_model=Envelope[List[AgentRead]])
async def list_agents(
    only_active: bool = False,
    svc: AgentService = Depends(get_agent_service),
) -> Envelope[List[AgentRead]]:
    rows = await svc.list_active() if only_active else await svc.list_all()
    return success_envelope([_to_read(a) for a in rows])


@router.get("/{agent_id}", response_model=Envelope[AgentRead])
async def get_agent(
    agent_id: str,
    svc: AgentService = Depends(get_agent_service),
) -> Envelope[AgentRead]:
    a = await svc.get(agent_id)
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return success_envelope(_to_read(a))


def _to_read(a: Agent) -> AgentRead:
    return AgentRead(
        agent_id=a.agent_id,
        name=a.name,
        owner_department=a.owner_dept,
        description=a.description,
        endpoint_url=a.endpoint_url,
        timeout_ms=a.timeout_ms,
        max_retry=a.max_retry,
        status=a.status,
        created_at=a.created_at,
    )
