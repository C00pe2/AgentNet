"""联邦:周期性把 peer registry 的 agent 同步为本地 federated 行。

同步后召回(本地 embedding)/精排/Gateway 代理全部复用现有链路:
- federated 行的 endpoint 指向 peer 的 Gateway(`/v1/agents/{id}`),调用经 peer 链式代理,
  agent credential 由 peer 持有,peer 侧同样记录调用与计费;
- 本 registry 对 peer 而言就是普通消费者(持配置的 consumer key);
- 巡检/canary 不作用于 federated 行(由 peer 自己治理);同名冲突时本地自有 agent 优先;
- 本地无调用记录时,信誉展示回退到同步下来的 peer 快照。
peer 不可达时本轮同步跳过(保留陈旧数据),永不影响主流程。
"""

from __future__ import annotations

import asyncio
import contextlib

from agentnet_core import AgentCard
from agentnet_core.enums import AgentStatus
from sqlalchemy import delete

from .config import PeerRegistry
from .db import AgentRow
from .embedding import card_embed_text

FEDERATED_PREFIX = "federated:"


def federated_provider(peer: PeerRegistry) -> str:
    return FEDERATED_PREFIX + (peer.name or peer.url)


class FederationSync:
    def __init__(self, session_maker, http, embedder, peers: list[PeerRegistry], interval_sec: float) -> None:
        self._sm = session_maker
        self._http = http
        self._embedder = embedder
        self._peers = peers
        self._interval = interval_sec
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            with contextlib.suppress(Exception):
                await self.tick()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)

    async def tick(self) -> None:
        for peer in self._peers:
            with contextlib.suppress(Exception):
                await self._sync_peer(peer)

    async def _sync_peer(self, peer: PeerRegistry) -> None:
        resp = await self._http.get(
            peer.url.rstrip("/") + "/v1/agents",
            headers={"Authorization": f"Bearer {peer.consumer_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        agents = resp.json()["data"]
        provider = federated_provider(peer)

        # embedding 用本地管线批量计算(与本地 agent 同一召回质量)
        cards = [AgentCard(**a) for a in agents]
        vecs = await self._embedder.embed([card_embed_text(c) for c in cards]) if cards else []

        async with self._sm() as session:
            seen: set[str] = set()
            for data, card, vec in zip(agents, cards, vecs, strict=True):
                seen.add(card.agent_id)
                row = await session.get(AgentRow, card.agent_id)
                if row is not None and not row.provider.startswith(FEDERATED_PREFIX):
                    continue  # 本地自有 agent 优先,不被联邦覆盖
                if row is None:
                    row = AgentRow(agent_id=card.agent_id, provider=provider)
                    session.add(row)
                row.provider = provider
                row.name = card.name
                row.description = card.description
                row.natural_capabilities = card.natural_capabilities
                row.capabilities = card.capabilities
                row.canary_cases = []  # federated 不在本地跑 canary(由来源 registry 治理)
                row.endpoint = f"{peer.url.rstrip('/')}/v1/agents/{card.agent_id}"
                row.auth_type = "bearer"
                row.auth_token = peer.consumer_key
                row.pricing = card.pricing.model_dump()
                row.version = card.version
                row.status = (
                    data.get("status")
                    if data.get("status") in (AgentStatus.ACTIVE.value, AgentStatus.OFFLINE.value)
                    else AgentStatus.ACTIVE.value
                )
                row.remote_reputation = data.get("reputation")
                row.embedding = vec
                row.consecutive_health_failures = 0
            # peer 上已消失的 federated 行同步删除(peer 不可达时不会走到这里)
            await session.execute(
                delete(AgentRow).where(
                    AgentRow.provider == provider, AgentRow.agent_id.not_in(seen or {""})
                )
            )
            await session.commit()
