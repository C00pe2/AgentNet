"""企业级复用度量 API (SPEC §6)。

指标 1: 跨部门网络联动对数
  Count(Distinct(agent_sessions.caller_dept -> agents.owner_dept))

指标 2: 公共资产复用深度
  - 复用广度: 某 Agent 关联的独立 caller_dept 数量
  - 调用跨度饱和度: 非本部门调用 / 全局 * 100%
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agent, AgentCallLog, AgentSession

logger = logging.getLogger(__name__)


class MetricsService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def cross_dept_linking_pairs(self) -> list[dict]:
        sm = self.session
        q = (
            select(
                AgentSession.caller_dept,
                Agent.owner_dept,
                func.count().label("link_count"),
            )
            .select_from(AgentCallLog)
            .join(Agent, Agent.agent_id == AgentCallLog.target_agent_id)
            .join(AgentSession, AgentSession.session_id == AgentCallLog.session_id)
            .group_by(AgentSession.caller_dept, Agent.owner_dept)
        )
        rows = (await sm.execute(q)).all()
        return [
            {
                "caller_dept": cd,
                "owner_dept": od,
                "link_count": int(cnt or 0),
            }
            for cd, od, cnt in rows
        ]

    async def reusability_depth(self) -> list[dict]:
        sm = self.session
        q_total = (
            select(
                AgentCallLog.target_agent_id,
                Agent.name,
                Agent.owner_dept,
                func.count().label("total_calls"),
                func.count(func.distinct(AgentSession.caller_dept)).label("unique_caller_depts"),
            )
            .select_from(AgentCallLog)
            .join(Agent, Agent.agent_id == AgentCallLog.target_agent_id)
            .join(AgentSession, AgentSession.session_id == AgentCallLog.session_id)
            .group_by(
                AgentCallLog.target_agent_id,
                Agent.name,
                Agent.owner_dept,
            )
        )
        total_rows = (await sm.execute(q_total)).all()

        q_cross = (
            select(
                AgentCallLog.target_agent_id,
                func.coalesce(
                    func.sum(
                        case(
                            (AgentSession.caller_dept != Agent.owner_dept, 1),
                            else_=0,
                        )
                    ),
                    0,
                ).label("cross_calls"),
            )
            .select_from(AgentCallLog)
            .join(Agent, Agent.agent_id == AgentCallLog.target_agent_id)
            .join(AgentSession, AgentSession.session_id == AgentCallLog.session_id)
            .group_by(AgentCallLog.target_agent_id)
        )
        cross_rows = (await sm.execute(q_cross)).all()
        cross_map = {r[0]: int(r[1] or 0) for r in cross_rows}

        items: list[dict] = []
        for agent_id, name, owner_dept, total, uniq in total_rows:
            total = int(total or 0)
            uniq = int(uniq or 0)
            cross = cross_map.get(agent_id, 0)
            saturation = (cross / total * 100.0) if total > 0 else 0.0
            items.append(
                {
                    "agent_id": agent_id,
                    "name": name,
                    "owner_dept": owner_dept,
                    "unique_caller_depts": uniq,
                    "total_calls": total,
                    "cross_dept_calls": cross,
                    "cross_dept_saturation_pct": round(saturation, 2),
                }
            )
        items.sort(key=lambda x: x["unique_caller_depts"], reverse=True)
        return items

    async def collect(self) -> dict:
        pairs = await self.cross_dept_linking_pairs()
        items = await self.reusability_depth()
        return {
            "cross_dept_linking_pairs": {"count": len(pairs), "pairs": pairs},
            "reusability_depth": {"items": items},
        }
