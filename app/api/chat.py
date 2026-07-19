"""/chat 主入口：fast_route → 单步直跳 or deep_plan → dispatcher。

SPEC §3 全链路。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.circuit_breaker import on_failure as cb_on_failure
from app.core.circuit_breaker import on_success as cb_on_success
from app.core.circuit_breaker import pre_call as cb_pre_call
from app.core.codes import ErrorCode, GatewayError, downstream_to_gateway_code
from app.core.ratelimit import get_rate_limiter
from app.db.models import Agent, AgentSession, PlanExecution
from app.db.session import get_sessionmaker
from app.deps import get_agent_service, get_session_service
from app.llm.client import get_llm_client
from app.schemas.response import error_envelope, success_envelope
from app.services.agent_service import AgentService
from app.services.deep_plan import deep_plan
from app.services.dispatcher import Dispatcher
from app.services.expert_client import get_expert_client
from app.services.fast_route import fast_route
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1)


@router.post("")
async def chat(
    payload: ChatRequest,
    agent_svc: AgentService = Depends(get_agent_service),
    session_svc: SessionService = Depends(get_session_service),
) -> dict[str, Any]:
    """主入口。

    1) 查询 session -> 必须存在，否则 400
    2) 拉取 active agents -> 若空，404 NO_AGENT_MATCHED
    3) 一层 fast_route
       - agent_id: 直跳下游 -> 写 call_log
       - COMPLEX/UNKNOWN: 进二层 deep_plan + Dispatcher
    4) 把所有错误归一到网关错误码 -> Envelope
    """
    session = await session_svc.get(payload.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_id 不存在")

    active_agents = await agent_svc.list_active()
    if not active_agents:
        return error_envelope(
            ErrorCode.NO_AGENT_MATCHED,
            "当前没有 active 的专家 Agent 在线",
        ).model_dump()

    llm = get_llm_client()
    decision, raw = await fast_route(
        user_query=payload.query,
        active_agents=active_agents,
        llm=llm,
    )

    if decision == "UNKNOWN":
        return error_envelope(
            ErrorCode.NO_AGENT_MATCHED,
            f"无可用 Agent 匹配该意图。fast_route raw={raw}",
        ).model_dump()

    agents_by_id = {a.agent_id: a for a in active_agents}

    if decision in agents_by_id:
        # 单步直跳
        plan_row = await _create_plan(
            session_id=payload.session_id,
            raw_query=payload.query,
            execution_graph={
                "mode": "fast_route_single",
                "decision": decision,
            },
        )
        result = await _dispatch_single(
            agent=agents_by_id[decision],
            query=payload.query,
            session=session,
            plan_id=plan_row.plan_id,
            session_id=payload.session_id,
        )
        if result["status"] == "SUCCESS":
            await _finalize_plan(plan_row.plan_id, success=True)
            return success_envelope(
                {"content": result["content"], "plan_id": plan_row.plan_id, "agent_id": decision}
            ).model_dump()
        await _finalize_plan(plan_row.plan_id, success=False)
        return error_envelope(
            ErrorCode(result["code"]),
            result.get("error") or "Agent 调用失败",
            data={"plan_id": plan_row.plan_id, "agent_id": decision},
        ).model_dump()

    if decision == "COMPLEX":
        plan_row = await _create_plan(
            session_id=payload.session_id,
            raw_query=payload.query,
            execution_graph={"mode": "deep_plan", "ready": True},
        )
        # 更新 graph 为 deep_plan 真实输出
        try:
            steps = await deep_plan(
                user_query=payload.query,
                active_agents=active_agents,
                llm=llm,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("deep_plan 失败: %s", exc)
            await _finalize_plan(plan_row.plan_id, success=False)
            return error_envelope(
                ErrorCode.PLAN_ORCHESTRATION_FAILED,
                f"Planner 解析失败: {exc}",
                data={"plan_id": plan_row.plan_id},
            ).model_dump()

        await _update_plan_graph(plan_row.plan_id, {"mode": "deep_plan", "steps": steps})
        # 复用 lifespan 启动的全局 ExpertAgentClient，避免重复起连接池
        dispatcher = Dispatcher(expert_client=get_expert_client())
        await dispatcher.start()
        try:
            dispatch_result = await dispatcher.dispatch(
                plan=plan_row,
                steps=steps,
                agents_by_id=agents_by_id,
                session_id=payload.session_id,
                caller_dept=session.caller_dept,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("dispatch 异常: %s", exc)
            await _finalize_plan(plan_row.plan_id, success=False)
            return error_envelope(
                ErrorCode.PLAN_ORCHESTRATION_FAILED,
                f"Dispatcher 异常: {exc}",
                data={"plan_id": plan_row.plan_id},
            ).model_dump()

        if dispatch_result.final_code == int(ErrorCode.SUCCESS):
            return success_envelope(
                {
                    "content": dispatch_result.final_content,
                    "plan_id": dispatch_result.plan_id,
                    "agent_id": dispatch_result.step_results[-1].agent_id if dispatch_result.step_results else None,
                    "steps": [
                        {
                            "step_id": s.step_id,
                            "agent_id": s.agent_id,
                            "code": s.code,
                            "status": s.status,
                            "latency_ms": s.latency_ms,
                        }
                        for s in dispatch_result.step_results
                    ],
                }
            ).model_dump()

        return error_envelope(
            ErrorCode(dispatch_result.final_code),
            dispatch_result.final_message,
            data={
                "plan_id": dispatch_result.plan_id,
                "steps": [
                    {
                        "step_id": s.step_id,
                        "agent_id": s.agent_id,
                        "code": s.code,
                        "status": s.status,
                        "latency_ms": s.latency_ms,
                    }
                    for s in dispatch_result.step_results
                ],
            },
        ).model_dump()

    # fast_route 返回了非法内容 (理论上不会发生，因为 _normalize 已经收敛)
    return error_envelope(
        ErrorCode.PLAN_ORCHESTRATION_FAILED,
        f"fast_route 决策无法识别: {decision}",
    ).model_dump()


# ---------------------------------------------------------
# 内部辅助方法 (不在 router 暴露)
# ---------------------------------------------------------

async def _dispatch_single(
    *,
    agent: Agent,
    query: str,
    session: AgentSession,
    plan_id: int,
    session_id: str,
) -> dict:
    """单步直跳，与 dispatcher 行为一致但只跑一步。"""

    # 限流
    try:
        await get_rate_limiter().check_and_consume(agent.agent_id)
    except GatewayError as exc:
        return {"status": "FAILED", "code": int(exc.code), "error": str(exc)}

    # 熔断预检
    try:
        await cb_pre_call(agent.agent_id)
    except GatewayError as exc:
        return {"status": "FAILED", "code": int(exc.code), "error": str(exc)}

    # SPEC §3.3: 对入参也走 token 强截断，保持一致行为
    from app.core.token_clip import clip_text_to_token_budget
    rendered_query = clip_text_to_token_budget(query)

    payload = {
        "agent_id": agent.agent_id,
        "sub_query": rendered_query,
        "context": {
            "session_id": session_id,
            "caller_dept": session.caller_dept,
            "plan_id": plan_id,
            "step_id": 0,
        },
    }
    expert = get_expert_client()
    t0 = time.monotonic()
    res = await expert.invoke(
        agent,
        payload=payload,
        caller_dept=session.caller_dept,
        timeout_ms=agent.timeout_ms,
        max_retry=agent.max_retry,
    )
    elapsed_ms = res.latency_ms
    if elapsed_ms == 0 and t0:
        elapsed_ms = int((time.monotonic() - t0) * 1000)

    gateway_code = downstream_to_gateway_code(res.http_status, timed_out=res.timed_out)
    success = gateway_code == int(ErrorCode.SUCCESS)

    # 审计
    await fire_and_forget(
        plan_id=plan_id,
        session_id=session_id,
        target_agent_id=agent.agent_id,
        step_id=0,
        request_payload=payload,
        result=res,
        code=int(gateway_code),
        status="SUCCESS" if success else "FAILED",
        latency_ms=res.latency_ms,
    )

    if success:
        cb_on_success(agent.agent_id)
        await _mark_requested(agent.agent_id)
        content = ""
        body = res.body or {}
        if isinstance(body, dict):
            data = body.get("data") or {}
            if isinstance(data, dict):
                content = data.get("content") or data.get("text") or ""
        return {"status": "SUCCESS", "code": 200, "content": content, "latency_ms": res.latency_ms}

    if res.timed_out or (res.http_status and res.http_status >= 500):
        cb_on_failure(agent.agent_id)

    if gateway_code == int(ErrorCode.AGENT_TIMEOUT):
        return {"status": "FAILED", "code": int(ErrorCode.AGENT_TIMEOUT), "error": "agent timeout"}
    if gateway_code == int(ErrorCode.BAD_REQUEST):
        return {"status": "FAILED", "code": int(ErrorCode.BAD_REQUEST), "error": "downstream 4xx"}
    return {"status": "FAILED", "code": int(ErrorCode.AGENT_5XX_ERROR), "error": res.error or "downstream error"}


async def fire_and_forget(
    *,
    plan_id: int | None,
    session_id: str,
    target_agent_id: str,
    step_id: int,
    request_payload: dict,
    result,
    code: int,
    status: str,
    latency_ms: int,
) -> None:
    from app.services.call_log_service import fire_and_forget_record

    await fire_and_forget_record(
        get_sessionmaker(),
        plan_id=plan_id,
        session_id=session_id,
        target_agent_id=target_agent_id,
        step_id=step_id,
        request_snapshot=request_payload,
        response_snapshot=(result.body or None),
        code=code,
        latency_ms=latency_ms,
        status=status,
    )


async def _create_plan(
    *,
    session_id: str,
    raw_query: str,
    execution_graph: dict,
) -> PlanExecution:
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            row = PlanExecution(
                session_id=session_id,
                raw_query=raw_query,
                execution_graph=execution_graph or {},
                status="RUNNING",
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return row


async def _update_plan_graph(plan_id: int, graph: dict) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            plan = await session.get(PlanExecution, plan_id)
            if plan is not None:
                plan.execution_graph = graph


async def _finalize_plan(plan_id: int, *, success: bool) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            plan = await session.get(PlanExecution, plan_id)
            if plan is None:
                return
            plan.status = "SUCCEEDED" if success else "FAILED"
            plan.finished_at = datetime.utcnow()


async def _mark_requested(agent_id: str) -> None:
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            async with session.begin():
                a = await session.get(Agent, agent_id)
                if a is not None:
                    a.last_request_at = datetime.utcnow()
    except Exception:  # noqa: BLE001
        logger.exception("更新 last_request_at 失败 %s", agent_id)
