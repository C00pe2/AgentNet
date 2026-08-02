"""Canary 跑分测试:通过/关键词缺失/宕机 → canary_logs + 信誉 canary_score。"""

from agentnet_registry.canary import CanaryRunner
from agentnet_registry.db import CanaryLogRow
from conftest import GO_CARD, install_world, make_key
from sqlalchemy import func, select

CANARY_CARD = {
    **GO_CARD,
    "canary_cases": [{"query": "这段 channel 用法有问题吗", "expect": ["goroutine", "channel"]}],
}


def _canary_spec(card: dict, answer: str, task_id: str = "c-1") -> dict:
    return {
        "card": card,
        "task": {
            "id": task_id,
            "agent_id": card["agent_id"],
            "state": "completed",
            "messages": [{"role": "agent", "parts": [{"type": "text", "text": answer}]}],
            "artifacts": [],
        },
        "sse": "",
    }


async def _register(client, provider, card) -> None:
    resp = await client.post("/v1/agents", json=card, headers=provider)
    assert resp.status_code == 200, resp.text


def _runner(app, timeout: float = 5.0) -> CanaryRunner:
    return CanaryRunner(app.state.session_maker, app.state.http, interval_sec=300, timeout_sec=timeout)


async def _canary_logs(app, agent_id: str = "go-reviewer") -> list:
    async with app.state.session_maker() as session:
        return (
            (await session.execute(select(CanaryLogRow).where(CanaryLogRow.agent_id == agent_id)))
            .scalars()
            .all()
        )


async def _reputation(client, headers, agent_id: str = "go-reviewer") -> dict:
    resp = await client.get(f"/v1/agents/{agent_id}", headers=headers)
    return resp.json()["data"]["reputation"]


async def test_canary_pass_scores_one(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": _canary_spec(CANARY_CARD, "发现 goroutine 泄漏,channel 未关闭")})
    provider = await make_key(client, "provider", "alice")
    await _register(client, provider, CANARY_CARD)

    await _runner(app).tick()

    logs = await _canary_logs(app)
    assert len(logs) == 1 and logs[0].passed is True and logs[0].detail == ""
    rep = await _reputation(client, provider)
    assert rep["canary_score"] == 1.0


async def test_canary_missing_keyword_fails(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": _canary_spec(CANARY_CARD, "一切正常")})
    provider = await make_key(client, "provider", "alice")
    await _register(client, provider, CANARY_CARD)

    await _runner(app).tick()

    logs = await _canary_logs(app)
    assert len(logs) == 1 and logs[0].passed is False and "缺少关键词" in logs[0].detail
    rep = await _reputation(client, provider)
    assert rep["canary_score"] == 0.0


async def test_canary_agent_down_records_failure(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": {"down": True}})
    provider = await make_key(client, "provider", "alice")
    # 注册验证需要 agent 在线:先在线注册,再切换为宕机
    install_world(app, {"go-reviewer.test": _canary_spec(CANARY_CARD, "ok goroutine channel")})
    await _register(client, provider, CANARY_CARD)
    install_world(app, {"go-reviewer.test": {"down": True}})

    await _runner(app).tick()

    logs = await _canary_logs(app)
    assert len(logs) == 1 and logs[0].passed is False and "ConnectError" in logs[0].detail


async def test_canary_skips_agents_without_cases(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": _canary_spec(GO_CARD, "anything")})
    provider = await make_key(client, "provider", "alice")
    await _register(client, provider, GO_CARD)  # 未声明 canary_cases

    await _runner(app).tick()

    async with app.state.session_maker() as session:
        count = await session.scalar(select(func.count()).select_from(CanaryLogRow))
    assert count == 0
