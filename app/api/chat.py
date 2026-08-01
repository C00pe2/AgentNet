"""/chat 主入口：fast_route → 单步 or deep_plan → 统一 Dispatcher 执行。

SPEC §3 全链路。单步直跳与 DAG 编排共享同一 Dispatcher，
保证限流 / 熔断 / 审计 / 错误归一行为完全一致。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.codes import ErrorCode
from app.core.token_clip import clip_text_to_token_budget
from app.db.models import PlanExecution
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

    1) 查询 session -> 必须存在，否则 404
    2) 拉取 active agents -> 若空，404 NO_AGENT_MATCHED
    3) 一层 fast_route
       - agent_id: 构造单 step 计划
       - COMPLEX: 二层 deep_plan 产出 DAG steps
       - UNKNOWN: 404 NO_AGENT_MATCHED
    4) 统一交给 Dispatcher 执行
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
        # 单步直跳：构造单 step 计划，与 DAG 共用 Dispatcher 全链路
        plan_row = await _create_plan(
            session_id=payload.session_id,
            raw_query=payload.query,
            execution_graph={
                "mode": "fast_route_single",
                "decision": decision,
            },
        )
        steps: list[dict[str, Any]] = [
            {
                "step_id": 0,
                "agent_id": decision,
                # SPEC §3.3: 入参同样走 2K token 强截断
                "sub_query": clip_text_to_token_budget(payload.query),
                "depends_on": [],
                "tool_call_id": None,
                "raw_arguments": {},
                "no_match": False,
                "reason": None,
            }
        ]
    elif decision == "COMPLEX":
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
    else:
        # fast_route 返回了非法内容 (理论上不会发生，因为 _normalize 已经收敛)
        return error_envelope(
            ErrorCode.PLAN_ORCHESTRATION_FAILED,
            f"fast_route 决策无法识别: {decision}",
        ).model_dump()

    # 统一执行: 复用 lifespan 启动的全局 ExpertAgentClient，避免重复起连接池
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

    steps_payload = [
        {
            "step_id": s.step_id,
            "agent_id": s.agent_id,
            "code": s.code,
            "status": s.status,
            "latency_ms": s.latency_ms,
        }
        for s in dispatch_result.step_results
    ]

    if dispatch_result.final_code == int(ErrorCode.SUCCESS):
        return success_envelope(
            {
                "content": dispatch_result.final_content,
                "plan_id": dispatch_result.plan_id,
                "agent_id": dispatch_result.step_results[-1].agent_id if dispatch_result.step_results else None,
                "steps": steps_payload,
            }
        ).model_dump()

    return error_envelope(
        ErrorCode(dispatch_result.final_code),
        dispatch_result.final_message,
        data={
            "plan_id": dispatch_result.plan_id,
            "steps": steps_payload,
        },
    ).model_dump()


# ---------------------------------------------------------
# 内部辅助方法 (不在 router 暴露)
# ---------------------------------------------------------

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
