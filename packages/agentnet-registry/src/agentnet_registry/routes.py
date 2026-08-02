"""Registry API 路由。

控制面:/v1/keys、/v1/agents(注册/查询/注销)、/v1/agents/search、/v1/feedback
数据面:/v1/agents/{id}/tasks...(Gateway 代理到 agent,落库调用记录)

注意路由顺序:/agents/search 必须先于 /agents/{agent_id} 声明。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from agentnet_core import AgentCard, AgentPricing, MessageAppend, TaskCreate, constants
from agentnet_core.enums import AgentStatus, TaskState
from agentnet_core.models import AgentAuth
from agentnet_core.sse import SseParser

from .db import AgentRow, ApiKeyRow, CallLogRow
from .embedding import card_embed_text
from .gateway import (
    AgentHttpClient,
    insert_call_log,
    map_downstream_error,
    record_terminal,
)
from .recall import recall_top_k
from .reputation import load_reputations
from .schemas import FeedbackRequest, KeyCreate
from .security import (
    Principal,
    generate_key,
    get_principal,
    hash_key,
    require_admin,
    require_consumer,
    require_provider,
)

router = APIRouter()


def ok(data=None) -> dict:
    return {"code": 0, "message": "ok", "data": data}


def _mask_card(row: AgentRow) -> AgentCard:
    """消费者视角的 card:掩码 endpoint 与凭证(Gateway 模式下不暴露)。"""
    return AgentCard(
        agent_id=row.agent_id,
        name=row.name,
        description=row.description,
        natural_capabilities=row.natural_capabilities,
        capabilities=row.capabilities or [],
        endpoint="",
        auth=AgentAuth(type="none"),
        pricing=AgentPricing(**(row.pricing or {})),
        version=row.version,
        provider=row.provider,
    )


async def _get_agent_or_404(request: Request, agent_id: str) -> AgentRow:
    sm = request.app.state.session_maker
    async with sm() as session:
        row = await session.get(AgentRow, agent_id)
    if row is None:
        raise HTTPException(404, f"agent {agent_id!r} 不存在")
    return row


def _agent_client(request: Request, row: AgentRow) -> AgentHttpClient:
    settings = request.app.state.settings
    return AgentHttpClient(request.app.state.http, row, timeout=settings.agent_timeout_sec)


# ---------------------------------------------------------------------------
# 控制面:API Key 签发
# ---------------------------------------------------------------------------


@router.post("/keys")
async def create_key(req: KeyCreate, request: Request, _: Principal = Depends(require_admin)):
    key = generate_key(req.role)
    sm = request.app.state.session_maker
    async with sm() as session:
        session.add(ApiKeyRow(key_hash=hash_key(key), role=req.role, name=req.name))
        await session.commit()
    # 明文仅此一次返回
    return ok({"key": key, "role": req.role, "name": req.name})


# ---------------------------------------------------------------------------
# 控制面:Agent 注册 / 查询 / 注销
# ---------------------------------------------------------------------------


@router.post("/agents")
async def register_agent(card: AgentCard, request: Request, p: Principal = Depends(require_provider)):
    if not card.endpoint:
        raise HTTPException(400, "注册时 endpoint 必填")

    # 1) 回调 GET /card 验证 agent 真实存在且身份一致
    client = _agent_client(request, _row_for_verify(card))
    try:
        remote = await client.get_card()
    except httpx.HTTPError as exc:
        raise map_downstream_error(exc) from exc
    remote_id = remote.get("agent_id")
    if remote_id != card.agent_id:
        raise HTTPException(400, f"agent /card 返回 agent_id={remote_id!r},与注册不符")

    # 2) 计算 embedding
    embedder = request.app.state.embedder
    vec = (await embedder.embed([card_embed_text(card)]))[0]

    # 3) upsert(同一 agent_id 仅所属 provider 可更新)
    sm = request.app.state.session_maker
    async with sm() as session:
        row = await session.get(AgentRow, card.agent_id)
        if row is not None and row.provider != p.name:
            raise HTTPException(403, "该 agent_id 已被其他 provider 注册")
        if row is None:
            row = AgentRow(agent_id=card.agent_id, provider=p.name)
            session.add(row)
        row.name = card.name
        row.description = card.description
        row.natural_capabilities = card.natural_capabilities
        row.capabilities = card.capabilities
        row.endpoint = card.endpoint
        row.auth_type = card.auth.type
        row.auth_token = card.auth.token
        row.pricing = card.pricing.model_dump()
        row.version = card.version
        row.embedding = vec
        row.status = AgentStatus.ACTIVE.value
        row.consecutive_health_failures = 0
        await session.commit()
    return ok({"agent_id": card.agent_id, "status": AgentStatus.ACTIVE.value})


def _row_for_verify(card: AgentCard) -> AgentRow:
    """构造临时 row 仅用于复用 AgentHttpClient 做注册验证。"""
    return AgentRow(
        agent_id=card.agent_id,
        provider="",
        name=card.name,
        endpoint=card.endpoint,
        auth_type=card.auth.type,
        auth_token=card.auth.token,
    )


# 注意:必须先于 /agents/{agent_id} 声明
@router.get("/agents/search")
async def search_agents(
    request: Request,
    q: str,
    top_k: int | None = None,
    _: Principal = Depends(require_consumer),
):
    settings = request.app.state.settings
    k = top_k or settings.recall_top_k
    embedder = request.app.state.embedder
    vec = (await embedder.embed([q]))[0]

    sm = request.app.state.session_maker
    async with sm() as session:
        rows = await recall_top_k(session, vec, k)
        reps = await load_reputations(session, [row.agent_id for row, _ in rows])

    candidates = []
    for row, dist in rows:
        card = _mask_card(row)
        card.reputation = reps.get(row.agent_id)
        candidates.append({"score": max(0.0, 1.0 - float(dist)), "card": card.model_dump(mode="json")})
    return ok({"query": q, "candidates": candidates})


@router.get("/agents")
async def list_agents(request: Request, _: Principal = Depends(get_principal)):
    sm = request.app.state.session_maker
    async with sm() as session:
        rows = (await session.execute(select(AgentRow).order_by(AgentRow.created_at))).scalars().all()
        reps = await load_reputations(session, [r.agent_id for r in rows])
    out = []
    for row in rows:
        card = _mask_card(row)
        card.reputation = reps.get(row.agent_id)
        data = card.model_dump(mode="json")
        data["status"] = row.status
        out.append(data)
    return ok(out)


@router.get("/agents/{agent_id}")
async def get_agent(request: Request, agent_id: str, _: Principal = Depends(get_principal)):
    row = await _get_agent_or_404(request, agent_id)
    sm = request.app.state.session_maker
    async with sm() as session:
        reps = await load_reputations(session, [agent_id])
    card = _mask_card(row)
    card.reputation = reps.get(agent_id)
    data = card.model_dump(mode="json")
    data["status"] = row.status
    return ok(data)


@router.delete("/agents/{agent_id}")
async def delete_agent(request: Request, agent_id: str, p: Principal = Depends(require_provider)):
    row = await _get_agent_or_404(request, agent_id)
    if row.provider != p.name:
        raise HTTPException(403, "只能注销自己注册的 agent")
    sm = request.app.state.session_maker
    async with sm() as session:
        await session.delete(row)
        await session.commit()
    return ok({"agent_id": agent_id, "deleted": True})


# ---------------------------------------------------------------------------
# 数据面:Gateway 代理(消费者 → Registry → Agent)
# ---------------------------------------------------------------------------


def _governance_precheck(request: Request, agent_id: str) -> None:
    if not request.app.state.rate_limiter.allow(agent_id):
        raise HTTPException(429, "该 agent 调用超出限流")
    if not request.app.state.breaker.pre_check(agent_id):
        raise HTTPException(503, "该 agent 处于熔断状态,请稍后再试")


async def _proxy_json(request: Request, row: AgentRow, method: str, path: str, payload: dict | None):
    """普通 JSON 端点代理 + 熔断反馈 + 错误归一。"""
    breaker = request.app.state.breaker
    client = _agent_client(request, row)
    try:
        resp = await (client.post(path, payload) if method == "POST" else client.get(path))
    except httpx.HTTPError as exc:
        breaker.on_failure(row.agent_id)
        raise map_downstream_error(exc) from exc
    if resp.status_code >= 500:
        breaker.on_failure(row.agent_id)
        raise HTTPException(502, f"agent 服务端错误({resp.status_code})")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"agent 拒绝请求: {resp.text[:200]}")
    breaker.on_success(row.agent_id)
    return resp.json()


@router.post("/agents/{agent_id}/tasks")
async def gw_create_task(
    agent_id: str, body: TaskCreate, request: Request, p: Principal = Depends(require_consumer)
):
    row = await _get_agent_or_404(request, agent_id)
    if row.status != AgentStatus.ACTIVE.value:
        raise HTTPException(503, "agent 当前离线")
    _governance_precheck(request, agent_id)
    payload = await _proxy_json(
        request, row, "POST", constants.TASKS_PATH, body.model_dump(mode="json")
    )
    task_id = payload.get("id")
    if task_id:
        await insert_call_log(request.app.state.session_maker, task_id, agent_id, p.name)
    return ok(payload)


@router.get("/agents/{agent_id}/tasks/{task_id}")
async def gw_get_task(agent_id: str, task_id: str, request: Request, _: Principal = Depends(require_consumer)):
    row = await _get_agent_or_404(request, agent_id)
    payload = await _proxy_json(request, row, "GET", constants.task_path(task_id), None)
    state = payload.get("state")
    if state:
        try:
            await record_terminal(request.app.state.session_maker, task_id, TaskState(state))
        except ValueError:
            pass  # agent 返回了协议外状态,忽略终态记录
    return ok(payload)


@router.post("/agents/{agent_id}/tasks/{task_id}/messages")
async def gw_append_message(
    agent_id: str, task_id: str, body: MessageAppend, request: Request, _: Principal = Depends(require_consumer)
):
    row = await _get_agent_or_404(request, agent_id)
    payload = await _proxy_json(
        request, row, "POST", constants.task_messages_path(task_id), body.model_dump(mode="json")
    )
    return ok(payload)


@router.post("/agents/{agent_id}/tasks/{task_id}/cancel")
async def gw_cancel_task(agent_id: str, task_id: str, request: Request, _: Principal = Depends(require_consumer)):
    row = await _get_agent_or_404(request, agent_id)
    payload = await _proxy_json(request, row, "POST", constants.task_cancel_path(task_id), {})
    await record_terminal(request.app.state.session_maker, task_id, TaskState.CANCELED)
    return ok(payload)


@router.get("/agents/{agent_id}/tasks/{task_id}/events")
async def gw_task_events(agent_id: str, task_id: str, request: Request, _: Principal = Depends(require_consumer)):
    """SSE 代理:透传事件流,同时旁观终态事件落库调用记录。"""
    row = await _get_agent_or_404(request, agent_id)
    settings = request.app.state.settings
    client = _agent_client(request, row)
    sm = request.app.state.session_maker

    try:
        req = client.build_events_request(task_id, timeout=settings.gateway_timeout_sec)
        resp = await request.app.state.http.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise map_downstream_error(exc) from exc
    if resp.status_code != 200:
        await resp.aclose()
        raise HTTPException(502, f"agent 事件流不可用({resp.status_code})")

    parser = SseParser()

    async def stream() -> AsyncIterator[str]:
        try:
            async for line in resp.aiter_lines():
                parsed = parser.feed(line)
                if parsed is not None:
                    event, data = parsed
                    try:
                        if event == constants.SSE_STATE:
                            state = json.loads(data).get("state")
                            await record_terminal(sm, task_id, TaskState(state))
                        elif event == constants.SSE_ERROR:
                            error = json.loads(data).get("error")
                            await record_terminal(sm, task_id, TaskState.FAILED, error)
                    except (ValueError, json.JSONDecodeError):
                        pass  # 协议外事件不影响转发
                yield line + "\n"
        finally:
            await resp.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# 控制面:消费者反馈(评分进信誉)
# ---------------------------------------------------------------------------


@router.post("/feedback")
async def submit_feedback(body: FeedbackRequest, request: Request, _: Principal = Depends(require_consumer)):
    sm = request.app.state.session_maker
    async with sm() as session:
        row = await session.scalar(select(CallLogRow).where(CallLogRow.task_id == body.task_id))
        if row is None:
            raise HTTPException(404, "task 不存在或无调用记录")
        row.rating = body.rating
        await session.commit()
    return ok({"task_id": body.task_id, "rating": body.rating})
