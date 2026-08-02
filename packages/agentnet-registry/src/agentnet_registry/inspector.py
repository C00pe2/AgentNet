"""巡检:周期性 GET /health + GET /card 一致性校验,连续失败达阈值标记 offline,恢复后自动回 active。

用 asyncio 后台任务实现,不引入额外调度依赖。巡检失败永不影响主流程。
"""

from __future__ import annotations

import asyncio
import contextlib

from agentnet_core.enums import AgentStatus
from sqlalchemy import select

from .db import AgentRow
from .gateway import AgentHttpClient


class Inspector:
    def __init__(self, session_maker, http, interval_sec: float, fail_threshold: int) -> None:
        self._sm = session_maker
        self._http = http
        self._interval = interval_sec
        self._fail_threshold = fail_threshold
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            with contextlib.suppress(Exception):
                await self.tick()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)

    async def _check_agent(self, row: AgentRow) -> bool:
        """health 通过且 /card 上报的 agent_id 与注册一致,才算健康。"""
        client = AgentHttpClient(self._http, row, timeout=3.0)
        if not await client.health():
            return False
        with contextlib.suppress(Exception):
            card = await client.get_card()
            return card.get("agent_id") == row.agent_id
        return False

    async def tick(self) -> None:
        async with self._sm() as session:
            agent_ids = (await session.execute(select(AgentRow.agent_id))).scalars().all()

        for agent_id in agent_ids:
            async with self._sm() as session:
                row = await session.get(AgentRow, agent_id)
                if row is None:
                    continue
                healthy = await self._check_agent(row)
                if healthy:
                    row.consecutive_health_failures = 0
                    row.status = AgentStatus.ACTIVE.value
                else:
                    row.consecutive_health_failures += 1
                    if row.consecutive_health_failures >= self._fail_threshold:
                        row.status = AgentStatus.OFFLINE.value
                await session.commit()
