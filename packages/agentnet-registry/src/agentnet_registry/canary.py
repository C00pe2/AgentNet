"""Canary 跑分:定期把 provider 声明的验证用例发给 agent,核对关键词,结果计入信誉。

防能力欺诈的硬手段:声称的能力必须能通过自己出的题。
与巡检一样用 asyncio 后台任务实现;canary 走控制面直连 agent,不进 Gateway 调用记录,
任何失败都只收敛为一条未通过记录,永不影响主流程。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from datetime import UTC, datetime

from agentnet_core import constants
from agentnet_core.enums import AgentStatus, TaskState
from sqlalchemy import select

from .db import AgentRow, CanaryLogRow
from .gateway import AgentHttpClient

_POLL_INTERVAL_SEC = 0.5
_TERMINAL = {TaskState.COMPLETED.value, TaskState.FAILED.value, TaskState.CANCELED.value}


class CanaryRunner:
    def __init__(self, session_maker, http, interval_sec: float, timeout_sec: float) -> None:
        self._sm = session_maker
        self._http = http
        self._interval = interval_sec
        self._timeout = timeout_sec
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
        """对所有 active 且声明了 canary 用例的 agent,各随机跑 1 个用例。"""
        async with self._sm() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentRow.agent_id, AgentRow.canary_cases).where(
                            AgentRow.status == AgentStatus.ACTIVE.value
                        )
                    )
                )
                .all()
            )
        for agent_id, cases in rows:
            if not cases:
                continue
            case = random.choice(cases)
            async with self._sm() as session:
                row = await session.get(AgentRow, agent_id)
                if row is None:
                    continue
                await self._run_case(session, row, case)

    async def _run_case(self, session, row: AgentRow, case: dict) -> None:
        client = AgentHttpClient(self._http, row, timeout=self._timeout)
        started = datetime.now(UTC)
        passed, detail = False, ""
        latency: int | None = None
        try:
            resp = await client.post(
                constants.TASKS_PATH,
                {"message": {"role": "user", "parts": [{"type": "text", "text": case["query"]}]}},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"创建任务被拒绝({resp.status_code})")
            task = await self._await_terminal(client, resp.json()["id"])
            latency = int((datetime.now(UTC) - started).total_seconds() * 1000)
            state = task.get("state")
            if state != TaskState.COMPLETED.value:
                detail = f"任务未成功(状态 {state})"
            else:
                answer = _last_agent_text(task)
                missing = [k for k in case["expect"] if k not in answer]
                passed = not missing
                detail = "" if passed else f"回答缺少关键词: {missing}"
        except Exception as exc:  # noqa: BLE001 - canary 失败收敛为未通过记录
            detail = f"{exc.__class__.__name__}: {exc}"
        session.add(
            CanaryLogRow(
                agent_id=row.agent_id,
                query=case["query"],
                passed=passed,
                detail=detail,
                latency_ms=latency,
            )
        )
        await session.commit()

    async def _await_terminal(self, client: AgentHttpClient, task_id: str) -> dict:
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            resp = await client.get(constants.task_path(task_id))
            resp.raise_for_status()
            task = resp.json()
            if task.get("state") in _TERMINAL:
                return task
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"canary 任务 {task_id} 超时未完成")
            await asyncio.sleep(_POLL_INTERVAL_SEC)


def _last_agent_text(task: dict) -> str:
    for msg in reversed(task.get("messages", [])):
        if msg.get("role") == "agent":
            return "".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text")
    return ""
