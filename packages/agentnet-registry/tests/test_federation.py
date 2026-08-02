"""联邦同步测试:upsert / 删除 / 本地优先 / 巡检跳过 / 信誉快照回退 / 召回复用。"""

import httpx
from agentnet_registry.config import PeerRegistry
from agentnet_registry.db import AgentRow
from agentnet_registry.embedding import HashEmbedding
from agentnet_registry.federation import FederationSync
from agentnet_registry.inspector import Inspector
from conftest import GO_CARD, install_world, make_card, make_key, spec_for

PEER = PeerRegistry(url="http://peer.test", consumer_key="peer-ck", name="p1")


def _remote_agent(agent_id: str = "sql-optimizer", reputation: dict | None = None) -> dict:
    card = make_card(agent_id, "SQL Optimizer", "SQL 查询优化,索引建议,执行计划分析", "x")
    return {
        **card,
        "status": "active",
        "provider": "remote-alice",
        "reputation": reputation or {"calls": 9, "success_rate": 0.9, "avg_latency_ms": 12.0, "rating": 4.0},
    }


def _peer_transport(agents: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agents":
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": agents})
        return httpx.Response(404, json={"code": 404, "message": "not found", "data": None})

    return httpx.MockTransport(handler)


def _sync(app, agents: list[dict]) -> FederationSync:
    return FederationSync(
        app.state.session_maker,
        httpx.AsyncClient(transport=_peer_transport(agents)),
        HashEmbedding(1024),
        [PEER],
        interval_sec=60,
    )


async def _row(app, agent_id: str) -> AgentRow | None:
    async with app.state.session_maker() as session:
        return await session.get(AgentRow, agent_id)


async def test_sync_upserts_federated_rows_and_recycles_pipeline(app_and_client):
    app, client = app_and_client
    await _sync(app, [_remote_agent()]).tick()

    row = await _row(app, "sql-optimizer")
    assert row.provider == "federated:p1"
    assert row.endpoint == "http://peer.test/v1/agents/sql-optimizer"  # 调用经 peer Gateway 链式代理
    assert row.auth_token == "peer-ck"
    assert row.embedding is not None  # 本地管线计算
    assert row.remote_reputation["calls"] == 9

    # 召回/搜索直接复用:federated agent 出现在结果里,信誉回退到 peer 快照
    consumer = await make_key(client, "consumer", "bob")
    resp = await client.get("/v1/agents/search", params={"q": "帮我优化慢 SQL"}, headers=consumer)
    candidates = resp.json()["data"]["candidates"]
    assert [c["card"]["agent_id"] for c in candidates] == ["sql-optimizer"]
    assert candidates[0]["card"]["reputation"]["calls"] == 9
    assert candidates[0]["card"]["provider"] == "federated:p1"


async def test_sync_deletes_disappeared_agents(app_and_client):
    app, _ = app_and_client
    agents = [_remote_agent()]
    sync = _sync(app, agents)
    await sync.tick()
    assert await _row(app, "sql-optimizer") is not None

    agents.clear()  # peer 上已注销
    await sync.tick()
    assert await _row(app, "sql-optimizer") is None


async def test_sync_never_overwrites_local_agent(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})
    provider = await make_key(client, "provider", "alice")
    resp = await client.post("/v1/agents", json=GO_CARD, headers=provider)
    assert resp.status_code == 200, resp.text

    # peer 上有个同名 agent:本地自有的优先,不被覆盖
    await _sync(app, [_remote_agent("go-reviewer")]).tick()
    row = await _row(app, "go-reviewer")
    assert row.provider == "alice"
    assert row.endpoint == "http://go-reviewer.test"


async def test_inspector_skips_federated_rows(app_and_client):
    app, _ = app_and_client
    await _sync(app, [_remote_agent()]).tick()
    install_world(app, {})  # 任何出站都失败;federated 行不应被巡检触碰

    insp = Inspector(app.state.session_maker, app.state.http, interval_sec=60, fail_threshold=1)
    await insp.tick()
    row = await _row(app, "sql-optimizer")
    assert row.status == "active" and row.consecutive_health_failures == 0


async def test_sync_failure_keeps_stale_rows(app_and_client):
    app, _ = app_and_client
    await _sync(app, [_remote_agent()]).tick()

    def down_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    sync = FederationSync(
        app.state.session_maker,
        httpx.AsyncClient(transport=httpx.MockTransport(down_handler)),
        HashEmbedding(1024),
        [PEER],
        interval_sec=60,
    )
    await sync.tick()  # peer 不可达:跳过,保留陈旧数据
    assert await _row(app, "sql-optimizer") is not None


async def test_federated_call_chain_unwraps_envelope(app_and_client):
    """经 federated 行的调用:peer 返回的 Envelope 被解包,任务字段透传给消费者。"""

    app, client = app_and_client

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": [_remote_agent()]})
        if path == "/v1/agents/sql-optimizer/tasks" and request.method == "POST":
            task = {"id": "ft-1", "state": "submitted", "messages": [], "artifacts": []}
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": task})
        return httpx.Response(404, json={"code": 404, "message": "not found", "data": None})

    transport = httpx.MockTransport(handler)
    sync = FederationSync(
        app.state.session_maker,
        httpx.AsyncClient(transport=transport),
        HashEmbedding(1024),
        [PEER],
        interval_sec=60,
    )
    await sync.tick()
    app.state.http = httpx.AsyncClient(transport=transport)  # 数据面出站也走 peer 世界

    consumer = await make_key(client, "consumer", "bob")
    resp = await client.post(
        "/v1/agents/sql-optimizer/tasks",
        json={"message": {"role": "user", "parts": [{"type": "text", "text": "q"}]}},
        headers=consumer,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "ft-1"  # 解包成功,不是嵌套 envelope
