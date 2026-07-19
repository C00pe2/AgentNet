"""熔断器 (SPEC §5.1)。

- 滑动窗口 WINDOW_SEC (默认 10s) 内累计 FAIL_THRESHOLD (默认 5) 次 408/502 -> 进入 OPEN
- OPEN 状态持续 OPEN_SEC (默认 60s)，期间请求直接抛 AGENT_5XX_ERROR
- HALF_OPEN: 时间过后允许一次探针；下一次调用成功 -> CLOSED，失败 -> OPEN 重新计时
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from enum import Enum
from threading import Lock

from app.config import get_settings
from app.core.codes import ErrorCode, GatewayError


class State(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        *,
        window_sec: int | None = None,
        fail_threshold: int | None = None,
        open_sec: int | None = None,
    ):
        s = get_settings()
        self.window_sec = window_sec if window_sec is not None else s.breaker_window_sec
        self.fail_threshold = (
            fail_threshold if fail_threshold is not None else s.breaker_fail_threshold
        )
        self.open_sec = open_sec if open_sec is not None else s.breaker_open_sec

        self._state: State = State.CLOSED
        self._opened_at: float = 0.0
        self._fail_ts: dict[str, deque[float]] = {}
        self._lock = Lock()

    def _now(self) -> float:
        return time.monotonic()

    def _trim(self, key: str, now: float) -> None:
        window = self._fail_ts.setdefault(key, deque())
        cutoff = now - self.window_sec
        while window and window[0] < cutoff:
            window.popleft()

    def _state_of(self, agent_id: str, now: float) -> State:
        if self._state == State.OPEN and now - self._opened_at >= self.open_sec:
            self._state = State.HALF_OPEN
        return self._state

    def pre_check(self, agent_id: str) -> None:
        """调用下游之前判断是否被熔断。OPEN 直接抛错。"""
        now = self._now()
        with self._lock:
            st = self._state_of(agent_id, now)
        if st == State.OPEN:
            raise GatewayError(
                ErrorCode.AGENT_5XX_ERROR,
                f"Agent {agent_id} 已被网关熔断 (剩余 {self._remaining_open_sec(now):.1f}s)",
            )

    def _remaining_open_sec(self, now: float) -> float:
        return max(0.0, self.open_sec - (now - self._opened_at))

    def record_success(self, agent_id: str) -> None:
        with self._lock:
            self._state = State.CLOSED
            self._fail_ts.pop(agent_id, None)

    def record_failure(self, agent_id: str) -> None:
        """下游出现 408/502 时调用。"""
        now = self._now()
        with self._lock:
            self._trim(agent_id, now)
            window = self._fail_ts.setdefault(agent_id, deque())
            window.append(now)
            self._state = self._state_of(agent_id, now)
            if self._state in (State.HALF_OPEN, State.OPEN):
                self._state = State.OPEN
                self._opened_at = now
            elif len(window) >= self.fail_threshold:
                self._state = State.OPEN
                self._opened_at = now

    def snapshot(self) -> dict[str, str]:
        return {
            "state": self._state.value,
            "remaining_open_sec": f"{self._remaining_open_sec(self._now()):.1f}",
        }


_SINGLETON: CircuitBreaker | None = None


def get_circuit_breaker() -> CircuitBreaker:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = CircuitBreaker()
    return _SINGLETON


async def pre_call(agent_id: str) -> None:
    breaker = get_circuit_breaker()
    await asyncio.to_thread(breaker.pre_check, agent_id)


def on_success(agent_id: str) -> None:
    get_circuit_breaker().record_success(agent_id)


def on_failure(agent_id: str) -> None:
    get_circuit_breaker().record_failure(agent_id)
