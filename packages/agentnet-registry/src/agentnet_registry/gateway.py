"""Gateway 数据面:Registry → Agent 的协议客户端 + 调用记录。

消费者永远只跟 Registry 通信;agent 的 endpoint 和 credential 由 Registry 持有,
调用经这里转发,成败/延迟由 Gateway 亲眼记录 —— 信誉数据的来源。
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from agentnet_core import constants
from agentnet_core.enums import TaskState

from .db import AgentRow, CallLogRow


class AgentHttpClient:
    """面向单个 agent 的协议客户端(自动携带该 agent 的凭证)。"""

    def __init__(self, http: httpx.AsyncClient, row: AgentRow, timeout: float) -> None:
        self._http = http
        self._endpoint = row.endpoint.rstrip("/")
        self._timeout = timeout
        self._headers: dict[str, str] = {}
        if row.auth_type == "bearer" and row.auth_token:
            self._headers["Authorization"] = f"Bearer {row.auth_token}"

    async def get_card(self) -> dict:
        resp = await self._http.get(
            self._endpoint + constants.CARD_PATH, headers=self._headers, timeout=self._timeout
        )
        resp.raise_for_status()
        return resp.json()

    async def health(self) -> bool:
        try:
            resp = await self._http.get(
                self._endpoint + constants.HEALTH_PATH, headers=self._headers, timeout=3.0
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def post(self, path: str, payload: dict) -> httpx.Response:
        return await self._http.post(
            self._endpoint + path, json=payload, headers=self._headers, timeout=self._timeout
        )

    async def get(self, path: str) -> httpx.Response:
        return await self._http.get(
            self._endpoint + path, headers=self._headers, timeout=self._timeout
        )

    def build_events_request(self, task_id: str, timeout: float) -> httpx.Request:
        return self._http.build_request(
            "GET",
            self._endpoint + constants.task_events_path(task_id),
            headers=self._headers,
            timeout=timeout,
        )


def map_downstream_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(408, "调用 agent 超时")
    return HTTPException(502, f"无法连接 agent({exc.__class__.__name__})")


async def insert_call_log(sm, task_id: str, agent_id: str, consumer: str) -> None:
    async with sm() as session:
        session.add(CallLogRow(task_id=task_id, agent_id=agent_id, consumer=consumer))
        await session.commit()


async def record_terminal(sm, task_id: str, state: TaskState, error: str | None = None) -> None:
    """任务到达终态时记录(只记一次;由 SSE 代理或轮询代理触发)。"""
    if not state.is_terminal:
        return
    async with sm() as session:
        row = await session.scalar(select(CallLogRow).where(CallLogRow.task_id == task_id))
        if row is None or row.final_state is not None:
            return
        now = datetime.now(timezone.utc)
        row.final_state = state.value
        row.finished_at = now
        created = row.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            row.latency_ms = int((now - created).total_seconds() * 1000)
        row.error = error
        await session.commit()
