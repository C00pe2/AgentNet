"""治理原语:每 agent 限流 + 熔断。

注意:旧版(v0 gateway)熔断器的 _state 是全局单值,一个 agent 熔断会挡掉
所有 agent。本实现所有状态都按 agent_id 分桶,并配有回归测试。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """每 agent 滑动窗口限流(次/秒)。"""

    def __init__(self, rps: int) -> None:
        self.rps = rps
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, agent_id: str) -> bool:
        now = time.monotonic()
        hits = self._hits[agent_id]
        while hits and now - hits[0] > 1.0:
            hits.popleft()
        if len(hits) >= self.rps:
            return False
        hits.append(now)
        return True


class CircuitBreaker:
    """每 agent 熔断器:窗口内失败达阈值 → 熔断 open_sec 秒,之后半开放行。"""

    def __init__(self, fail_threshold: int = 5, open_sec: float = 60.0, window_sec: float = 10.0) -> None:
        self.fail_threshold = fail_threshold
        self.open_sec = open_sec
        self.window_sec = window_sec
        self._fail_ts: dict[str, deque[float]] = defaultdict(deque)
        self._opened_at: dict[str, float] = {}

    def pre_check(self, agent_id: str) -> bool:
        """True = 放行;False = 熔断中。"""
        opened = self._opened_at.get(agent_id)
        if opened is None:
            return True
        if time.monotonic() - opened >= self.open_sec:
            del self._opened_at[agent_id]  # 半开:放行一次试试
            self._fail_ts.pop(agent_id, None)
            return True
        return False

    def on_success(self, agent_id: str) -> None:
        self._fail_ts.pop(agent_id, None)
        self._opened_at.pop(agent_id, None)

    def on_failure(self, agent_id: str) -> None:
        now = time.monotonic()
        fails = self._fail_ts[agent_id]
        fails.append(now)
        while fails and now - fails[0] > self.window_sec:
            fails.popleft()
        if len(fails) >= self.fail_threshold:
            self._opened_at[agent_id] = now
            fails.clear()
