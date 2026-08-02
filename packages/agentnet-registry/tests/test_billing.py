"""计费测试:充值/余额预检(402)/ 成功扣费 / 失败不扣费。"""

from conftest import ADMIN, GO_CARD, install_world, make_key, spec_for

PAID_CARD = {**GO_CARD, "pricing": {"model": "per-call", "price": 2.0, "currency": "CNY"}}


async def _register(client, provider, card) -> None:
    resp = await client.post("/v1/agents", json=card, headers=provider)
    assert resp.status_code == 200, resp.text


async def _topup(client, name: str, amount: float) -> float:
    resp = await client.post("/v1/credits/topup", json={"name": name, "amount": amount}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["balance"]


async def _balance(client, consumer) -> dict:
    resp = await client.get("/v1/credits/balance", headers=consumer)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _create_task(client, consumer, agent_id: str = "go-reviewer"):
    return await client.post(
        f"/v1/agents/{agent_id}/tasks",
        json={"message": {"role": "user", "parts": [{"type": "text", "text": "q"}]}},
        headers=consumer,
    )


async def _drain_events(client, consumer, agent_id: str = "go-reviewer", task_id: str = "task-1"):
    """消费 SSE 到终态,触发 record_terminal(扣费挂点)。"""
    async with client.stream(
        "GET", f"/v1/agents/{agent_id}/tasks/{task_id}/events", headers=consumer
    ) as resp:
        async for _ in resp.aiter_lines():
            pass


async def test_topup_and_balance(app_and_client):
    _, client = app_and_client
    consumer = await make_key(client, "consumer", "bob")
    assert await _topup(client, "bob", 5.0) == 5.0
    assert await _topup(client, "bob", 3.0) == 8.0  # 累加
    data = await _balance(client, consumer)
    assert data["balance"] == 8.0
    assert [t["reason"] for t in data["transactions"]] == ["topup", "topup"]


async def test_paid_agent_insufficient_balance_402(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(PAID_CARD)})
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")
    await _register(client, provider, PAID_CARD)

    resp = await _create_task(client, consumer)  # 从未充值,余额 0
    assert resp.status_code == 402
    assert "余额不足" in resp.json()["message"]


async def test_paid_agent_charged_on_completed(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(PAID_CARD)})
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")
    await _register(client, provider, PAID_CARD)
    await _topup(client, "bob", 5.0)

    resp = await _create_task(client, consumer)
    assert resp.status_code == 200, resp.text
    await _drain_events(client, consumer)  # sse 终态 completed → 扣费

    data = await _balance(client, consumer)
    assert data["balance"] == 3.0  # 5 - 2
    charge = next(t for t in data["transactions"] if t["reason"] == "charge")
    assert charge["delta"] == -2.0 and charge["task_id"] == "task-1"


async def test_failed_task_not_charged(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(PAID_CARD, terminal="failed")})
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")
    await _register(client, provider, PAID_CARD)
    await _topup(client, "bob", 5.0)

    resp = await _create_task(client, consumer)
    assert resp.status_code == 200, resp.text
    await _drain_events(client, consumer)

    data = await _balance(client, consumer)
    assert data["balance"] == 5.0  # 失败不扣费
    assert all(t["reason"] == "topup" for t in data["transactions"])


async def test_free_agent_never_needs_balance(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})  # 默认 free
    provider = await make_key(client, "provider", "alice")
    consumer = await make_key(client, "consumer", "bob")
    await _register(client, provider, GO_CARD)

    resp = await _create_task(client, consumer)  # 余额 0 也可调用
    assert resp.status_code == 200, resp.text
