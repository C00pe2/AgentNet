"""分层健康检查服务 (SPEC §5.2)。

策略:
  - 核心: 近 health_core_idle_minutes (默认 60) 内有真实请求 -> 每 core_interval_sec (默认 30s) 探一次
  - 边缘: 超过 edge_idle_minutes (默认 1440 = 24h) 无请求 -> 每 edge_interval_sec (默认 300s) 探一次
  - 判定失败: HTTP 非 200 / 连接被拒 / 单次延迟 > health_probe_timeout_ms (默认 2000)
  - 隔离: 连续 health_fail_threshold (默认 3) 次失败 -> status='offline'，二层路由自动剔除

实现:
  - 上次探测时间戳用 in-memory dict 维护 (不需要新加 DB 字段)
  - tick_all() 由 scheduler 每秒拉一次；它内部按 agent 自己的 interval 决定是否真正探测
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Agent
from app.db.session import get_sessionmaker

logger = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: int
    detail: str


class HealthService:
    def __init__(self):
        s = get_settings()
        self.timeout_ms = s.health_probe_timeout_ms
        self.fail_threshold = s.health_fail_threshold
        self.core_idle_minutes = s.health_core_idle_minutes
        self.edge_idle_minutes = s.health_edge_idle_minutes
        self._client: httpx.AsyncClient | None = None
        self._last_probed_at: dict[str, float] = {}

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_ms / 1000.0)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _interval_for(self, agent: Agent, *, now: datetime) -> int:
        s = get_settings()
        if agent.last_request_at is None:
            return s.health_edge_interval_sec
        idle_minutes = (now - agent.last_request_at).total_seconds() / 60
        if idle_minutes <= self.core_idle_minutes:
            return s.health_core_interval_sec
        return s.health_edge_interval_sec

    def _is_due(self, agent: Agent, *, now_monotonic: float) -> bool:
        interval = self._interval_for(agent, now=datetime.utcnow())
        last = self._last_probed_at.get(agent.agent_id)
        if last is None:
            return True
        return (now_monotonic - last) >= interval

    async def probe(self, agent: Agent) -> ProbeResult:
        """单次 /health 探测。不考虑"上一次失败多少次"，仅算一次结果。"""
        assert self._client is not None
        base = agent.endpoint_url.rstrip("/")
        url = base + "/health" if not base.endswith("/health") else base
        t0 = time.monotonic()
        try:
            r = await self._client.get(url)
        except httpx.TimeoutException:
            return ProbeResult(
                ok=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail="timeout",
            )
        except httpx.HTTPError as exc:
            return ProbeResult(
                ok=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                detail=f"network:{type(exc).__name__}",
            )
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200 or latency_ms > self.timeout_ms:
            return ProbeResult(
                ok=False,
                latency_ms=latency_ms,
                detail=f"http={r.status_code}",
            )
        return ProbeResult(ok=True, latency_ms=latency_ms, detail="ok")

    async def probe_and_apply(self, agent: Agent) -> ProbeResult:
        """主动探测并把结果写回 DB。

        - 成功: consecutive_health_failures=0；若原状态 offline -> 切回 active
        - 失败: 计数 +1；若达到阈值 -> offline
        """
        result = await self.probe(agent)
        sm = get_sessionmaker()
        async with sm() as session:
            async with session.begin():
                fresh = await session.get(Agent, agent.agent_id)
                if fresh is None:
                    return result
                if result.ok:
                    fresh.consecutive_health_failures = 0
                    if fresh.status != "active":
                        fresh.status = "active"
                else:
                    fresh.consecutive_health_failures = (
                        fresh.consecutive_health_failures or 0
                    ) + 1
                    if (
                        fresh.consecutive_health_failures >= self.fail_threshold
                        and fresh.status != "offline"
                    ):
                        fresh.status = "offline"
                        logger.warning(
                            "Agent %s 连续失败 %s 次，置为 offline (detail=%s)",
                            fresh.agent_id,
                            fresh.consecutive_health_failures,
                            result.detail,
                        )
        return result

    async def tick_all(self) -> list[tuple[str, ProbeResult]]:
        """scheduler 每秒调用：对每个 due 的 agent 探测一次。"""
        await self.start()
        sm = get_sessionmaker()
        async with sm() as session:
            rows: Sequence[Agent] = (
                await session.execute(select(Agent))
            ).scalars().all()
        now_m = time.monotonic()
        out: list[tuple[str, ProbeResult]] = []
        for agent in rows:
            if not self._is_due(agent, now_monotonic=now_m):
                continue
            self._last_probed_at[agent.agent_id] = now_m
            result = await self.probe_and_apply(agent)
            out.append((agent.agent_id, result))
        return out
