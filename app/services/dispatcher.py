"""Dispatcher: DAG 编排执行器 (SPEC §3.3 + §3.4 + §4.1)。

执行模型:
  - 输入: deep_plan 返回的 steps 列表 + session_id + raw_query + active_agents
  - 对步骤做 Kahn 拓扑排序分层
  - 同层并发; 不同层串行 (上一层全部成功才进入下一层)
  - 占位符替换: 把 sub_query 中的 {{steps.N.output}} 替换为已完成 step 的 content,
     替换前对 content 做 BPE token 截断到配置上限 (默认 2048 tokens, SPEC §3.3)
  - Fail-Fast: 任意一步 (历经 max_retry) 超时/崩溃 -> 立即标记 plan=FAILED,
     中断后续所有节点, 返回 503 PLAN_ORCHESTRATION_FAILED
  - 整体返回: Envelope-shape 的 dict + 中间结果
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.core.circuit_breaker import on_failure as cb_on_failure
from app.core.circuit_breaker import on_success as cb_on_success
from app.core.circuit_breaker import pre_call as cb_pre_call
from app.core.codes import (
    ErrorCode,
    GatewayError,
    downstream_to_gateway_code,
)
from app.core.ratelimit import get_rate_limiter
from app.core.token_clip import clip_text_to_token_budget
from app.db.models import Agent, PlanExecution
from app.db.session import get_sessionmaker
from app.services.call_log_service import fire_and_forget_record
from app.services.expert_client import ExpertAgentClient, ExpertCallResult

logger = logging.getLogger(__name__)


_PLACEHOLDER_RE = re.compile(r"\{\{steps\.(\d+)\.output\}\}")


@dataclass
class StepOutcome:
    step_id: int
    agent_id: str | None
    code: int
    content: str
    latency_ms: int
    status: str  # SUCCESS / FAILED
    timed_out: bool
    downstream_status: int | None
    no_match: bool
    reason: str | None = None


@dataclass
class DispatchResult:
    final_code: int
    final_message: str
    final_content: str
    step_results: list[StepOutcome]
    plan_id: int | None
    timed_out: bool = False
    no_match: bool = False


class Dispatcher:
    """DAG 执行器。无状态; 通过 expert_client / rate_limiter / breaker 依赖注入。"""

    def __init__(self, expert_client: ExpertAgentClient | None = None):
        self.expert_client = expert_client or ExpertAgentClient()

    async def start(self) -> None:
        await self.expert_client.start()

    async def close(self) -> None:
        await self.expert_client.close()

    async def dispatch(
        self,
        *,
        plan: PlanExecution,
        steps: list[dict[str, Any]],
        agents_by_id: dict[str, Agent],
        session_id: str,
        caller_dept: str,
    ) -> DispatchResult:
        plan_id = plan.plan_id

        if not steps:
            return DispatchResult(
                final_code=ErrorCode.NO_AGENT_MATCHED,
                final_message="无可用编排步骤",
                final_content="",
                step_results=[],
                plan_id=plan_id,
                no_match=True,
            )

        # 1. 处理 no_match
        if steps and steps[0].get("no_match"):
            step = steps[0]
            await self._finalize_plan(plan_id, success=False)
            outcome = StepOutcome(
                step_id=0,
                agent_id=None,
                code=int(ErrorCode.NO_AGENT_MATCHED),
                content="",
                latency_ms=0,
                status="FAILED",
                timed_out=False,
                downstream_status=None,
                no_match=True,
                reason=step.get("reason") or "Planner 判定无可用 Agent",
            )
            return DispatchResult(
                final_code=int(ErrorCode.NO_AGENT_MATCHED),
                final_message=outcome.reason or "无可用 Agent",
                final_content="",
                step_results=[outcome],
                plan_id=plan_id,
                no_match=True,
            )

        # 2. Kahn 分层
        layers = _topo_layers(steps)

        results_by_step: dict[int, StepOutcome] = {}
        aborted = False
        aborted_reason_code: int | None = None
        aborted_reason_msg: str | None = None

        for layer in layers:
            if aborted:
                break
            tasks = [
                self._run_step(
                    step=step,
                    plan=plan,
                    session_id=session_id,
                    caller_dept=caller_dept,
                    agents_by_id=agents_by_id,
                    results_by_step=results_by_step,
                )
                for step in layer
            ]



            outcomes = await asyncio.gather(*tasks)
            for outcome in outcomes:
                results_by_step[outcome.step_id] = outcome
                if outcome.status != "SUCCESS":
                    aborted = True
                    if outcome.code == int(ErrorCode.ROUTE_LIMIT_EXCEEDED):
                        aborted_reason_code = outcome.code
                        aborted_reason_msg = f"step {outcome.step_id} 触发网关限流: {outcome.reason or ''}"
                    elif outcome.code == int(ErrorCode.BAD_REQUEST):
                        # 下游 4xx: 与原单跳行为一致，透传 BAD_REQUEST
                        aborted_reason_code = outcome.code
                        aborted_reason_msg = f"step {outcome.step_id} 下游拒绝请求 (4xx): {outcome.reason or ''}"
                    elif outcome.code == int(ErrorCode.AGENT_5XX_ERROR):
                        # 下游 5xx: 与原单跳行为一致，透传 AGENT_5XX_ERROR
                        aborted_reason_code = outcome.code
                        aborted_reason_msg = f"step {outcome.step_id} 下游 5xx: {outcome.reason or ''}"
                    elif outcome.timed_out:
                        aborted_reason_code = int(ErrorCode.AGENT_TIMEOUT)
                        aborted_reason_msg = f"step {outcome.step_id} 超时"
                    else:
                        aborted_reason_code = int(ErrorCode.PLAN_ORCHESTRATION_FAILED)
                        aborted_reason_msg = f"step {outcome.step_id} 执行失败: {outcome.reason or ''}"
                    break

        ordered = [results_by_step[s["step_id"]] for s in steps if s["step_id"] in results_by_step]

        if aborted:
            await self._finalize_plan(plan_id, success=False)
            code = aborted_reason_code or int(ErrorCode.PLAN_ORCHESTRATION_FAILED)
            message = aborted_reason_msg or "Plan 执行失败"
            content = ordered[-1].content if ordered else ""
            return DispatchResult(
                final_code=code,
                final_message=message,
                final_content=content,
                step_results=ordered,
                plan_id=plan_id,
                timed_out=code == int(ErrorCode.AGENT_TIMEOUT),
            )

        # 全部 step 成功
        await self._finalize_plan(plan_id, success=True)
        return DispatchResult(
            final_code=int(ErrorCode.SUCCESS),
            final_message="Success",
            final_content=ordered[-1].content if ordered else "",
            step_results=ordered,
            plan_id=plan_id,
        )

    async def _run_step(
        self,
        *,
        step: dict[str, Any],
        plan: PlanExecution,
        session_id: str,
        caller_dept: str,
        agents_by_id: dict[str, Agent],
        results_by_step: dict[int, StepOutcome],
    ) -> StepOutcome:
        agent_id = step.get("agent_id")
        sub_query_tmpl = step.get("sub_query") or ""
        step_id = int(step["step_id"])

        if agent_id is None or agent_id not in agents_by_id:
            return StepOutcome(
                step_id=step_id,
                agent_id=agent_id,
                code=int(ErrorCode.PLAN_ORCHESTRATION_FAILED),
                content="",
                latency_ms=0,
                status="FAILED",
                timed_out=False,
                downstream_status=None,
                no_match=False,
                reason=f"Planner 给出的 agent_id={agent_id} 不在 active 集合",
            )

        agent = agents_by_id[agent_id]
        # 限流
        try:
            await get_rate_limiter().check_and_consume(agent_id)
        except GatewayError as exc:
            return StepOutcome(
                step_id=step_id,
                agent_id=agent_id,
                code=int(ErrorCode.ROUTE_LIMIT_EXCEEDED),
                content="",
                latency_ms=0,
                status="FAILED",
                timed_out=False,
                downstream_status=None,
                no_match=False,
                reason=str(exc),
            )
        # 熔断预检
        try:
            await cb_pre_call(agent_id)
        except GatewayError as exc:
            return StepOutcome(
                step_id=step_id,
                agent_id=agent_id,
                code=int(ErrorCode.AGENT_5XX_ERROR),
                content="",
                latency_ms=0,
                status="FAILED",
                timed_out=False,
                downstream_status=None,
                no_match=False,
                reason=str(exc),
            )

        # 占位符替换 + token 强截断 (SPEC §3.3)
        rendered = _render_sub_query(
            template=sub_query_tmpl,
            results_by_step=results_by_step,
        )

        request_payload = {
            "agent_id": agent_id,
            "sub_query": rendered,
            "context": {
                "session_id": session_id,
                "caller_dept": caller_dept,
                "plan_id": plan.plan_id,
                "step_id": step_id,
            },
        }

        # 实际调用下游
        result = await self.expert_client.invoke(
            agent,
            payload=request_payload,
            caller_dept=caller_dept,
            timeout_ms=agent.timeout_ms,
            max_retry=agent.max_retry,
        )
        outcome = _to_outcome(step_id=step_id, agent_id=agent_id, result=result)

        # 熔断反馈
        if outcome.status == "SUCCESS":
            cb_on_success(agent_id)
            # 标一次"最近被请求"
            await _mark_requested(agent_id)
        else:
            # AGENT_TIMEOUT / AGENT_5XX_ERROR 才参与熔断计数；ROUTE_LIMIT/PARSE 不计入
            if outcome.timed_out or outcome.downstream_status and outcome.downstream_status >= 500:
                cb_on_failure(agent_id)

        # 异步写审计
        await fire_and_forget_record(
            get_sessionmaker(),
            plan_id=plan.plan_id,
            session_id=session_id,
            target_agent_id=agent_id,
            step_id=step_id,
            request_snapshot=request_payload,
            response_snapshot=(result.body or None),
            code=outcome.code,
            latency_ms=outcome.latency_ms,
            status=outcome.status,
        )

        return outcome

    async def _finalize_plan(
        self,
        plan_id: int,
        *,
        success: bool,
    ) -> None:
        sm = get_sessionmaker()
        async with sm() as session:
            async with session.begin():
                plan = await session.get(PlanExecution, plan_id)
                if plan is None:
                    return
                plan.status = "SUCCEEDED" if success else "FAILED"
                plan.finished_at = datetime.utcnow()


def _topo_layers(steps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Kahn 拓扑分层。返回 [ [step_a, step_b], [step_c], ... ]"""
    n = len(steps)
    in_deg = [0] * n
    step_by_id = {s["step_id"]: s for s in steps}
    edges: dict[int, list[int]] = {s["step_id"]: [] for s in steps}
    for s in steps:
        for dep in s.get("depends_on") or []:
            if dep in step_by_id and dep != s["step_id"]:
                edges[dep].append(s["step_id"])
                in_deg[s["step_id"]] += 1

    layers: list[list[dict[str, Any]]] = []
    remaining = list(steps)
    while remaining:
        layer: list[dict[str, Any]] = []
        for s in remaining:
            if in_deg[s["step_id"]] == 0:
                layer.append(s)
        if not layer:
            # 循环依赖 (防御)
            raise ValueError("Plan DAG 存在循环依赖")
        for s in layer:
            for child in edges[s["step_id"]]:
                in_deg[child] -= 1
        remaining = [s for s in remaining if s not in layer]
        layers.append(layer)
    return layers


def _render_sub_query(
    *,
    template: str,
    results_by_step: dict[int, StepOutcome],
) -> str:
    """把 {{steps.N.output}} 替换为已完成 step 的 output，并对每个替换值做 2K token 截断。"""

    def _sub(match: "re.Match[str]") -> str:
        idx = int(match.group(1))
        prev = results_by_step.get(idx)
        if prev is None:
            return ""
        raw = prev.content or ""
        budget = get_settings().step_output_token_limit
        return clip_text_to_token_budget(raw, token_limit=budget)

    return _PLACEHOLDER_RE.sub(_sub, template)


def _to_outcome(
    *, step_id: int, agent_id: str, result: ExpertCallResult
) -> StepOutcome:
    gateway_code = downstream_to_gateway_code(
        result.http_status, timed_out=result.timed_out
    )
    content = ""
    if result.body and isinstance(result.body, dict):
        # 标准网关契约: data.content -> 取出来作为下游的"输出"
        data = result.body.get("data") or {}
        if isinstance(data, dict):
            content = (
                data.get("content")
                or data.get("text")
                or data.get("message")
                or ""
            )
        if not content:
            content = result.body.get("content") or ""

    success = gateway_code == int(ErrorCode.SUCCESS)
    return StepOutcome(
        step_id=step_id,
        agent_id=agent_id,
        code=int(gateway_code),
        content=content,
        latency_ms=result.latency_ms,
        status="SUCCESS" if success else "FAILED",
        timed_out=result.timed_out,
        downstream_status=result.http_status if result.http_status else None,
        no_match=False,
        reason=result.error if not success else None,
    )


async def _mark_requested(agent_id: str) -> None:
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            async with session.begin():
                a = await session.get(Agent, agent_id)
                if a is not None:
                    a.last_request_at = datetime.utcnow()
    except Exception:  # noqa: BLE001
        logger.exception("更新 agent.last_request_at 失败 %s", agent_id)
