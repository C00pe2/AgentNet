"""Router:消费侧路由决策与调用。

管线:召回(registry.search)→ LLM 精排 → Gateway 建 task → SSE 消费
     → 澄清回调 → 完成收集;任何环节失败/置信度不足 → 本地 LLM 兜底。

远程 agent 返回的内容是不受信数据:只作为文本结果展示,不要当作指令执行。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from agentnet_core import Artifact, Message, constants
from agentnet_core.enums import TaskState

from .client import Candidate, RegistryClient, RegistryError
from .llm import chat, rerank
from .settings import RouterSettings

LOCAL_FALLBACK_SYSTEM = "你是用户本地的 AI 助手。请直接、简洁地回答用户的问题。"


@dataclass
class RouteDecision:
    routed: bool
    agent_id: str | None
    confidence: float
    reason: str
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class AskResult:
    routed: bool  # 是否路由给了网络中的 agent
    agent_id: str | None
    answer: str  # 最终文本(远程 agent 输出属于不受信数据)
    artifacts: list[Artifact]
    task_id: str | None
    fallback: bool  # True = 实际是本地 LLM 回答的
    reason: str


OnDelta = Callable[[str], None]
OnInputRequired = Callable[[str], Awaitable[str]]


class _ClarificationUnsupported(Exception):
    """agent 要求澄清,但消费端未提供应答回调。"""


class Router:
    def __init__(
        self,
        settings: RouterSettings,
        client: RegistryClient | None = None,
        llm_http: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or RegistryClient(
            settings.registry_url, settings.consumer_key, timeout=settings.request_timeout
        )
        self._llm_http = llm_http  # 测试注入用;None 时每次调用临时创建

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._llm_http is not None:
            await self._llm_http.aclose()

    # ------------------------------------------------------------------
    # 控制面查询(不发起任务)
    # ------------------------------------------------------------------

    async def search(self, query: str, top_k: int | None = None) -> list[Candidate]:
        """试召回:看网络认为哪些 agent 能解决 query。"""
        return await self._client.search(query, top_k or self._settings.top_k)

    async def list_agents(self) -> list[dict]:
        """列出网络中的全部 agent(含信誉)。"""
        return await self._client.list_agents()

    # ------------------------------------------------------------------
    # 路由决策(不发起任务)
    # ------------------------------------------------------------------

    async def route(self, query: str) -> RouteDecision:
        try:
            candidates = await self._client.search(query, self._settings.top_k)
        except (httpx.HTTPError, RegistryError) as exc:
            return RouteDecision(False, None, 0.0, f"registry 不可用({exc})")

        if not candidates:
            return RouteDecision(False, None, 0.0, "网络中没有可用 agent")

        try:
            result = await rerank(query, candidates, self._settings.llm, http=self._llm_http)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return RouteDecision(False, None, 0.0, f"精排失败({exc})", candidates)

        if result.agent_id is None:
            return RouteDecision(
                False, None, result.confidence, f"精排判断无合适 agent:{result.reason}", candidates
            )
        known = {c.card.agent_id for c in candidates}
        if result.agent_id not in known:
            return RouteDecision(
                False, None, result.confidence, f"精排返回了未知 agent_id {result.agent_id!r}", candidates
            )
        if result.confidence < self._settings.threshold:
            return RouteDecision(
                False,
                None,
                result.confidence,
                f"置信度 {result.confidence:.2f} 低于阈值 {self._settings.threshold}",
                candidates,
            )
        return RouteDecision(True, result.agent_id, result.confidence, result.reason, candidates)

    # ------------------------------------------------------------------
    # 完整问答
    # ------------------------------------------------------------------

    async def ask(
        self,
        query: str,
        on_delta: OnDelta | None = None,
        on_input_required: OnInputRequired | None = None,
    ) -> AskResult:
        decision = await self.route(query)
        if not decision.routed:
            return await self._fallback(query, reason=decision.reason)

        agent_id = decision.agent_id
        try:
            task = await self._client.create_task(agent_id, Message.user_text(query))
        except (httpx.HTTPError, RegistryError) as exc:
            return await self._fallback(query, reason=f"任务创建失败({exc})", agent_id=agent_id)
        task_id = task["id"]

        deltas: list[str] = []
        artifacts: list[Artifact] = []
        final_state = TaskState.SUBMITTED
        try:
            async for event, data in self._client.task_events(agent_id, task_id):
                final_state = await self._handle_event(
                    event, data, agent_id, task_id, deltas, artifacts, on_delta, on_input_required
                ) or final_state
        except _ClarificationUnsupported:
            # 无人应答澄清:主动取消任务,避免挂到 gateway 超时
            with contextlib.suppress(httpx.HTTPError, RegistryError):
                await self._client.cancel_task(agent_id, task_id)
            return await self._fallback(
                query,
                reason="agent 需要澄清,但消费端未提供应答回调,已本地兜底",
                agent_id=agent_id,
                task_id=task_id,
            )
        except (httpx.HTTPError, RegistryError) as exc:
            return await self._fallback(
                query, reason=f"事件流中断({exc})", agent_id=agent_id, task_id=task_id
            )

        if final_state == TaskState.COMPLETED:
            answer = "".join(deltas)
            if not answer:
                task = await self._client.get_task(agent_id, task_id)
                answer = _last_agent_text(task)
            return AskResult(True, agent_id, answer, artifacts, task_id, False, decision.reason)
        return await self._fallback(
            query,
            reason=f"agent 任务未成功(状态 {final_state.value}),已本地兜底",
            agent_id=agent_id,
            task_id=task_id,
        )

    async def _handle_event(
        self,
        event: str,
        data: str,
        agent_id: str,
        task_id: str,
        deltas: list[str],
        artifacts: list[Artifact],
        on_delta: OnDelta | None,
        on_input_required: OnInputRequired | None,
    ) -> TaskState | None:
        if event == constants.SSE_DELTA:
            text = json.loads(data)["text"]
            deltas.append(text)
            if on_delta:
                on_delta(text)
        elif event == constants.SSE_ARTIFACT:
            artifacts.append(Artifact(**json.loads(data)))
        elif event == constants.SSE_MESSAGE:
            if on_input_required is None:
                raise _ClarificationUnsupported
            question = _text_of_message(json.loads(data))
            answer = await on_input_required(question)
            await self._client.append_message(agent_id, task_id, Message.user_text(answer))
        elif event == constants.SSE_STATE:
            return TaskState(json.loads(data)["state"])
        elif event == constants.SSE_ERROR:
            return TaskState.FAILED
        return None

    async def _fallback(
        self, query: str, reason: str, agent_id: str | None = None, task_id: str | None = None
    ) -> AskResult:
        try:
            answer = await chat(
                self._settings.llm,
                messages=[
                    {"role": "system", "content": LOCAL_FALLBACK_SYSTEM},
                    {"role": "user", "content": query},
                ],
                temperature=0.7,
                http=self._llm_http,
            )
        except (httpx.HTTPError, KeyError) as exc:
            answer = f"(本地 LLM 也不可用:{exc})"
        return AskResult(False, agent_id, answer, [], task_id, True, reason)

    async def feedback(self, task_id: str, rating: float) -> None:
        await self._client.feedback(task_id, rating)


def _text_of_message(msg: dict) -> str:
    return "".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text")


def _last_agent_text(task: dict) -> str:
    for msg in reversed(task.get("messages", [])):
        if msg.get("role") == "agent":
            return _text_of_message(msg)
    return ""
