"""下发到专家 Agent 的 HTTP 客户端 (SPEC §2.3)。

强制注入:
  - X-AgentNet-Token: 从注册时拿到的静态令牌
  - X-Caller-Dept: 从 session 中拿到的部门标识

下游响应统一收敛为 (http_status, body_or_none, latency_ms, timed_out) 四元组，
由 Dispatcher 再根据 http_status + timed_out 通过 downstream_to_gateway_code() 归一。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.db.models import Agent

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 15.0


class ExpertAgentClient:
    """无状态 HTTP 客户端。使用 shared 连接池。"""

    def __init__(
        self,
        *,
        connection_limit: int = 200,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        follow_redirects: bool = False,
    ):
        self._timeout_s = timeout_s
        self._limits = httpx.Limits(
            max_connections=connection_limit,
            max_keepalive_connections=connection_limit,
        )
        self._follow_redirects = follow_redirects
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout_s,
                limits=self._limits,
                follow_redirects=self._follow_redirects,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def invoke(
        self,
        agent: Agent,
        *,
        payload: dict[str, Any],
        caller_dept: str,
        timeout_ms: int | None = None,
        max_retry: int | None = None,
    ) -> ExpertCallResult:
        """同步单次调用 (含 max_retry 次重试)。"""
        if self._client is None:
            await self.start()

        base_timeout_s = (timeout_ms or agent.timeout_ms or 15000) / 1000.0
        retries = (
            max_retry
            if max_retry is not None
            else (agent.max_retry if agent.max_retry is not None else 1)
        )

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return await self._do_call(
                    agent=agent,
                    payload=payload,
                    caller_dept=caller_dept,
                    timeout_s=base_timeout_s,
                )
            except GatewayHTTPError as exc:
                last_err = exc
                # 4xx 不重试 (网关层归一后是 400/502/408: 仅 5xx + 超时可重试)
                if 400 <= exc.http_status < 500:
                    return ExpertCallResult.from_error(exc, elapsed_ms=exc.latency_ms)
                if attempt >= retries:
                    return ExpertCallResult.from_error(exc, elapsed_ms=exc.latency_ms)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= retries:
                    return ExpertCallResult.from_exception(exc)

        # fallthrough, shouldn't reach
        return ExpertCallResult.from_exception(last_err or RuntimeError("unknown"))

    async def _do_call(
        self,
        *,
        agent: Agent,
        payload: dict[str, Any],
        caller_dept: str,
        timeout_s: float,
    ) -> "ExpertCallResult":
        assert self._client is not None
        url = agent.endpoint_url
        headers = {
            "Content-Type": "application/json",
            "X-AgentNet-Token": agent.auth_token,
            "X-Caller-Dept": caller_dept,
            "User-Agent": "AgentNet-Gateway/1.0",
        }
        if not _safe_url(url):
            raise GatewayHTTPError(
                http_status=502,
                latency_ms=0,
                message=f"Invalid endpoint url for agent {agent.agent_id}: {url}",
            )

        t0 = time.monotonic()
        try:
            resp = await self._client.post(
                url, json=payload, headers=headers, timeout=timeout_s
            )
        except httpx.TimeoutException as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return ExpertCallResult.from_timeout(latency_ms=latency_ms)
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return ExpertCallResult.from_exception(exc, latency_ms=latency_ms)

        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}

        if resp.status_code == 200:
            return ExpertCallResult.ok(http_status=200, body=body, latency_ms=latency_ms)
        return ExpertCallResult.from_error(
            GatewayHTTPError(
                http_status=resp.status_code,
                latency_ms=latency_ms,
                message=f"downstream returned {resp.status_code}",
            ),
            elapsed_ms=latency_ms,
            body=body,
        )


class GatewayHTTPError(Exception):
    def __init__(self, *, http_status: int, latency_ms: int, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.message = message


class ExpertCallResult:
    """下游调用的标准结果。"""

    def __init__(
        self,
        *,
        http_status: int,
        latency_ms: int,
        timed_out: bool,
        body: dict[str, Any] | None,
        error: str | None = None,
    ):
        self.http_status = http_status
        self.latency_ms = latency_ms
        self.timed_out = timed_out
        self.body = body
        self.error = error

    @classmethod
    def ok(cls, *, http_status: int, body: dict[str, Any], latency_ms: int) -> "ExpertCallResult":
        return cls(
            http_status=http_status,
            latency_ms=latency_ms,
            timed_out=False,
            body=body,
        )

    @classmethod
    def from_timeout(cls, *, latency_ms: int) -> "ExpertCallResult":
        return cls(
            http_status=0,
            latency_ms=latency_ms,
            timed_out=True,
            body=None,
            error="timeout",
        )

    @classmethod
    def from_exception(cls, exc: Exception, *, latency_ms: int = 0) -> "ExpertCallResult":
        return cls(
            http_status=0,
            latency_ms=latency_ms,
            timed_out=False,
            body=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    @classmethod
    def from_error(
        cls,
        exc: "GatewayHTTPError",
        *,
        elapsed_ms: int,
        body: dict[str, Any] | None = None,
    ) -> "ExpertCallResult":
        return cls(
            http_status=exc.http_status,
            latency_ms=elapsed_ms,
            timed_out=False,
            body=body,
            error=exc.message,
        )


def _safe_url(url: str) -> bool:
    try:
        u = urlparse(url)
    except Exception:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


_SINGLETON: ExpertAgentClient | None = None


def get_expert_client() -> ExpertAgentClient:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = ExpertAgentClient()
    return _SINGLETON


async def close_expert_client() -> None:
    if _SINGLETON is not None:
        await _SINGLETON.close()
        _SINGLETON = None
