"""Registry API 集成测试(需要本地 postgres;agent 侧用 MockTransport)。"""


from conftest import (
    GO_CARD,
    SQL_CARD,
    TRANSLATE_CARD,
    install_world,
    make_card,
    make_key,
    requires_db,
    spec_for,
)

pytestmark = [requires_db]


async def test_healthz(app_and_client):
    _, client = app_and_client
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["code"] == 0


async def test_key_issuance_and_authz(app_and_client):
    _, client = app_and_client
    # 无 key → 401
    assert (await client.post("/v1/keys", json={"role": "provider", "name": "x"})).status_code == 401
    # admin 签发
    provider = await make_key(client, "provider", "alice")
    # provider 不能签发 key(需 admin)→ 403
    resp = await client.post("/v1/keys", json={"role": "consumer", "name": "y"}, headers=provider)
    assert resp.status_code == 403
    # 错误 key → 401
    resp = await client.get("/v1/agents", headers={"Authorization": "Bearer an_fake_nope"})
    assert resp.status_code == 401


async def test_register_and_search_and_mask(app_and_client):
    app, client = app_and_client
    install_world(app, {
        "go-reviewer.test": spec_for(GO_CARD),
        "translator.test": spec_for(TRANSLATE_CARD),
        "sql-optimizer.test": spec_for(SQL_CARD),
    })
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")

    for card in (GO_CARD, TRANSLATE_CARD, SQL_CARD):
        resp = await client.post("/v1/agents", json=card, headers=provider)
        assert resp.status_code == 200, resp.text

    # 召回排序:go 查询 → go-reviewer 第一
    params = {"q": "帮我 review 这段 go 代码有没有并发问题"}
    resp = await client.get("/v1/agents/search", params=params, headers=consumer)
    assert resp.status_code == 200, resp.text
    candidates = resp.json()["data"]["candidates"]
    assert candidates[0]["card"]["agent_id"] == "go-reviewer"
    assert candidates[0]["score"] > candidates[-1]["score"]

    # 翻译查询 → translator 第一
    resp = await client.get("/v1/agents/search", params={"q": "帮我把这段英文文档翻译成中文"}, headers=consumer)
    assert resp.json()["data"]["candidates"][0]["card"]["agent_id"] == "translator"

    # 掩码:endpoint 与凭证绝不下发
    card = candidates[0]["card"]
    assert card["endpoint"] == ""
    assert card["auth"]["type"] == "none"
    assert card["auth"]["token"] is None


async def test_register_verification_mismatch(app_and_client):
    app, client = app_and_client
    bad = dict(spec_for(GO_CARD))
    bad["card"] = {**GO_CARD, "agent_id": "someone-else"}  # /card 返回与注册不符
    install_world(app, {"go-reviewer.test": bad})
    provider = await make_key(client, "provider", "alice")
    resp = await client.post("/v1/agents", json=GO_CARD, headers=provider)
    assert resp.status_code == 400


async def test_register_unreachable_agent(app_and_client):
    app, client = app_and_client
    spec = spec_for(GO_CARD)
    spec["down"] = True
    install_world(app, {"go-reviewer.test": spec})
    provider = await make_key(client, "provider", "alice")
    resp = await client.post("/v1/agents", json=GO_CARD, headers=provider)
    assert resp.status_code == 502


async def test_gateway_lifecycle_reputation_feedback(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD, task_id="task-1")})
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")
    await client.post("/v1/agents", json=GO_CARD, headers=provider)

    # 创建任务(经 Gateway 代理)
    resp = await client.post(
        "/v1/agents/go-reviewer/tasks",
        json={"message": {"role": "user", "parts": [{"type": "text", "text": "review 下"}]}},
        headers=consumer,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == "task-1"

    # 消费 SSE(Registry 旁观终态 → 落库调用记录)
    resp = await client.get("/v1/agents/go-reviewer/tasks/task-1/events", headers=consumer)
    assert resp.status_code == 200
    assert 'event: state\ndata: {"state": "completed"}' in resp.text

    # 信誉:1 次调用,成功率 1.0
    resp = await client.get("/v1/agents/search", params={"q": "go 代码审查"}, headers=consumer)
    rep = resp.json()["data"]["candidates"][0]["card"]["reputation"]
    assert rep["calls"] == 1
    assert rep["success_rate"] == 1.0
    assert rep["avg_latency_ms"] >= 0

    # 反馈评分进信誉
    resp = await client.post("/v1/feedback", json={"task_id": "task-1", "rating": 5}, headers=consumer)
    assert resp.status_code == 200
    resp = await client.get("/v1/agents/go-reviewer", headers=consumer)
    assert resp.json()["data"]["reputation"]["rating"] == 5.0


async def test_provider_isolation(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})
    alice = await make_key(client, "provider", "alice")
    mallory = await make_key(client, "provider", "mallory")
    consumer = await make_key(client, "consumer", "bob")

    await client.post("/v1/agents", json=GO_CARD, headers=alice)
    # 别人的 provider 不能覆盖注册
    resp = await client.post("/v1/agents", json=GO_CARD, headers=mallory)
    assert resp.status_code == 403
    # 别人的 provider 不能注销
    resp = await client.delete("/v1/agents/go-reviewer", headers=mallory)
    assert resp.status_code == 403
    # consumer 不能注册
    resp = await client.post("/v1/agents", json=make_card("x", "x", "x", "x.test"), headers=consumer)
    assert resp.status_code == 403
    # 属主可以注销
    resp = await client.delete("/v1/agents/go-reviewer", headers=alice)
    assert resp.status_code == 200


async def test_offline_agent_hidden_and_unavailable(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")
    await client.post("/v1/agents", json=GO_CARD, headers=provider)

    # 直接把 DB 里的状态改成 offline(模拟巡检效果)
    from agentnet_registry.db import AgentRow
    from sqlalchemy import update

    async with app.state.session_maker() as session:
        await session.execute(update(AgentRow).where(AgentRow.agent_id == "go-reviewer").values(status="offline"))
        await session.commit()

    resp = await client.get("/v1/agents/search", params={"q": "go"}, headers=consumer)
    assert resp.json()["data"]["candidates"] == []
    resp = await client.post(
        "/v1/agents/go-reviewer/tasks",
        json={"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        headers=consumer,
    )
    assert resp.status_code == 503
