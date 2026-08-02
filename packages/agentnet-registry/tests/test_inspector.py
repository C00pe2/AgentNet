"""巡检测试:health 宕机与 /card 不一致都会驱动 offline,恢复后自动回 active。"""

from agentnet_registry.db import AgentRow
from agentnet_registry.inspector import Inspector
from conftest import GO_CARD, install_world, make_key, spec_for


async def _register(client, provider) -> None:
    resp = await client.post("/v1/agents", json=GO_CARD, headers=provider)
    assert resp.status_code == 200, resp.text


async def _status(app, agent_id: str = "go-reviewer") -> str:
    async with app.state.session_maker() as session:
        return (await session.get(AgentRow, agent_id)).status


def _inspector(app, threshold: int = 2) -> Inspector:
    return Inspector(app.state.session_maker, app.state.http, interval_sec=60, fail_threshold=threshold)


async def test_inspector_marks_offline_and_recovers(app_and_client):
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})
    provider = await make_key(client, "provider", "alice")
    await _register(client, provider)
    assert await _status(app) == "active"

    # 宕机:连续失败达阈值才 offline(单次抖动不误判)
    install_world(app, {"go-reviewer.test": {"down": True}})
    insp = _inspector(app)
    await insp.tick()
    assert await _status(app) == "active"
    await insp.tick()
    assert await _status(app) == "offline"

    # 恢复 → 自动回 active
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})
    await _inspector(app).tick()
    assert await _status(app) == "active"


async def test_inspector_card_mismatch_counts_as_failure(app_and_client):
    """health 正常但 /card 上报的 agent_id 与注册不一致 → 同样计为失败。"""
    app, client = app_and_client
    install_world(app, {"go-reviewer.test": spec_for(GO_CARD)})
    provider = await make_key(client, "provider", "alice")
    await _register(client, provider)

    # 注册后,agent 上报的 card 变成另一个身份(欺诈/部署错误)
    wrong = {**GO_CARD, "agent_id": "impostor"}
    install_world(app, {"go-reviewer.test": spec_for(wrong)})
    insp = _inspector(app)
    await insp.tick()
    await insp.tick()
    assert await _status(app) == "offline"
