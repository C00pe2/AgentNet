"""内存级单 Agent 限流器 (SPEC §5.1)。

策略:
  - 每个 agent_id 维护一个以毫秒为精度的滑动窗口计数器
  - window=1s, 最大请求数 = RPS_LIMIT (默认 20)
  - 超过直接抛 ROUTE_LIMIT_EXCEEDED

注意: 这是进程内 in-memory state，多进程网关需要替换为 Redis。
MVP 阶段 / 内网单机部署足够。
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from threading import Lock

from app.config import get_settings
from app.core.codes import ErrorCode, GatewayError


class AgentRateLimiter:
    def __init__(self, rps: int | None = None):
        s = get_settings()
        self.capacity = rps if rps is not None else s.agent_rps_limit
        self._buckets: dict[str, deque[float]] = {}
        self._lock = Lock()

    def acquire(self, agent_id: str) -> None:
        """同步判断。在异步上下文调用由 check_and_consume 包装。"""
        now = time.monotonic()
        cutoff = now - 1.0
        with self._lock:
            bucket = self._buckets.setdefault(agent_id, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.capacity:
                raise GatewayError(
                    ErrorCode.ROUTE_LIMIT_EXCEEDED,
                    f"Agent {agent_id} 触发网关限流 (RPS>{self.capacity})",
                )
            bucket.append(now)

    async def check_and_consume(self, agent_id: str) -> None:
        # acquire 内部纯内存操作耗时 < 1ms，run_in_executor 没必要
        # 但仍给 await 友好
        await asyncio.to_thread(self.acquire, agent_id)


_RL_SINGLETON: AgentRateLimiter | None = None


def get_rate_limiter() -> AgentRateLimiter:
    global _RL_SINGLETON
    if _RL_SINGLETON is None:
        _RL_SINGLETON = AgentRateLimiter()
    return _RL_SINGLETON
