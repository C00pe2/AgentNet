"""Registry 客户端:召回、Gateway 任务代理、SSE 消费、反馈。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from agentnet_core import AgentCard, Message, SseParser, constants


@dataclass
class Candidate:
    score: float
    card: AgentCard


class RegistryClient:
    def __init__(
        self,
        base_url: str,
        consumer_key: str,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + constants.API_PREFIX
        self._headers = {"Authorization": f"Bearer {consumer_key}"}
        self._timeout = timeout
        self._http = httpx.AsyncClient(headers=self._headers, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text[:200])
            except Exception:  # noqa: BLE001
                message = resp.text[:200]
            raise RegistryError(resp.status_code, message)
        return resp

    async def list_agents(self) -> list[dict]:
        resp = self._check(await self._http.get(f"{self._base}/agents", timeout=self._timeout))
        return resp.json()["data"]

    async def search(self, query: str, top_k: int) -> list[Candidate]:
        resp = self._check(
            await self._http.get(
                f"{self._base}/agents/search",
                params={"q": query, "top_k": top_k},
                timeout=self._timeout,
            )
        )
        return [
            Candidate(score=item["score"], card=AgentCard(**item["card"]))
            for item in resp.json()["data"]["candidates"]
        ]

    async def create_task(self, agent_id: str, message: Message) -> dict:
        resp = self._check(
            await self._http.post(
                f"{self._base}/agents/{agent_id}/tasks",
                json={"message": message.model_dump(mode="json")},
                timeout=self._timeout,
            )
        )
        return resp.json()["data"]

    async def get_task(self, agent_id: str, task_id: str) -> dict:
        resp = self._check(
            await self._http.get(
                f"{self._base}/agents/{agent_id}/tasks/{task_id}", timeout=self._timeout
            )
        )
        return resp.json()["data"]

    async def append_message(self, agent_id: str, task_id: str, message: Message) -> dict:
        resp = self._check(
            await self._http.post(
                f"{self._base}/agents/{agent_id}/tasks/{task_id}/messages",
                json={"message": message.model_dump(mode="json")},
                timeout=self._timeout,
            )
        )
        return resp.json()["data"]

    async def cancel_task(self, agent_id: str, task_id: str) -> dict:
        resp = self._check(
            await self._http.post(
                f"{self._base}/agents/{agent_id}/tasks/{task_id}/cancel",
                timeout=self._timeout,
            )
        )
        return resp.json()["data"]

    async def feedback(self, task_id: str, rating: float) -> None:
        self._check(
            await self._http.post(
                f"{self._base}/feedback",
                json={"task_id": task_id, "rating": rating},
                timeout=self._timeout,
            )
        )

    async def task_events(self, agent_id: str, task_id: str) -> AsyncIterator[tuple[str, str]]:
        """消费 SSE 事件流,yield (event, data);流在 agent 到达终态后结束。"""
        parser = SseParser()
        async with self._http.stream(
            "GET",
            f"{self._base}/agents/{agent_id}/tasks/{task_id}/events",
            timeout=None,
        ) as resp:
            self._check(resp)
            async for line in resp.aiter_lines():
                if parsed := parser.feed(line):
                    yield parsed


class RegistryError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"registry 返回 {status_code}: {message}")
        self.status_code = status_code
