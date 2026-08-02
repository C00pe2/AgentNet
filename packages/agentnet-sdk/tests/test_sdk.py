"""AgentServer 测试:7 个协议端点 + 状态机 + SSE + ask/cancel/auth/失败。"""

import asyncio
import json

import httpx
import pytest
from agentnet_core import AgentCard, SseParser, TextPart
from agentnet_sdk import AgentServer

AUTH = {"Authorization": "Bearer secret"}


def make_server(auth_token: str | None = None) -> AgentServer:
    card = AgentCard(
        agent_id="demo",
        name="Demo",
        auth={"type": "bearer", "token": auth_token} if auth_token else {"type": "none"},
    )
    return AgentServer(card)


def client_for(server: AgentServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://agent.test"
    )


async def collect_events(client: httpx.AsyncClient, task_id: str, headers=None) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    async with client.stream("GET", f"/tasks/{task_id}/events", headers=headers or {}) as resp:
        parser = SseParser()
        async for line in resp.aiter_lines():
            if parsed := parser.feed(line):
                events.append(parsed)
    return events


async def wait_state(client: httpx.AsyncClient, task_id: str, state: str, timeout: float = 3.0) -> dict:
    async def _poll():
        while True:
            resp = await client.get(f"/tasks/{task_id}")
            data = resp.json()
            if data["state"] == state:
                return data
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout)


async def create_task(client: httpx.AsyncClient, text: str = "问题", headers=None) -> str:
    resp = await client.post(
        "/tasks",
        json={"message": {"role": "user", "parts": [{"type": "text", "text": text}]}},
        headers=headers or {},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_card_and_health():
    server = make_server()
    async with client_for(server) as client:
        assert (await client.get("/card")).json()["agent_id"] == "demo"
        assert (await client.get("/health")).json()["status"] == "ok"


async def test_task_flow_emit_and_return():
    server = make_server()

    @server.skill
    async def handle(ctx):
        await ctx.emit("分析中 ")
        return "最终答案"

    async with client_for(server) as client:
        task_id = await create_task(client)
        events = await collect_events(client, task_id)
        kinds = [e for e, _ in events]
        assert kinds[0] == "state"  # 订阅即补发当前状态
        assert ("delta", '{"text": "分析中 "}') in events
        assert json.loads(events[-1][1])["state"] == "completed"

        task = (await client.get(f"/tasks/{task_id}")).json()
        assert task["state"] == "completed"
        assert task["messages"][-1]["parts"][0]["text"] == "最终答案"


async def test_asyncgen_handler_aggregates_deltas():
    server = make_server()

    @server.skill
    async def handle(_ctx):
        yield "a"
        yield "b"

    async with client_for(server) as client:
        task_id = await create_task(client)
        await collect_events(client, task_id)
        task = (await client.get(f"/tasks/{task_id}")).json()
        assert task["state"] == "completed"
        assert task["messages"][-1]["parts"][0]["text"] == "ab"


async def test_ask_flow():
    server = make_server()

    @server.skill
    async def handle(ctx):
        branch = await ctx.ask("要 review 哪个分支?")
        return f"分支 {branch} 已审查"

    async with client_for(server) as client:
        task_id = await create_task(client)
        task = await wait_state(client, task_id, "input-required")
        assert task["messages"][-1]["parts"][0]["text"] == "要 review 哪个分支?"

        resp = await client.post(
            f"/tasks/{task_id}/messages",
            json={"message": {"role": "user", "parts": [{"type": "text", "text": "main"}]}},
        )
        assert resp.status_code == 200
        task = await wait_state(client, task_id, "completed")
        assert task["messages"][-1]["parts"][0]["text"] == "分支 main 已审查"


async def test_cancel_running_task():
    server = make_server()

    @server.skill
    async def handle(_ctx):
        await asyncio.sleep(60)

    async with client_for(server) as client:
        task_id = await create_task(client)
        await wait_state(client, task_id, "working")
        resp = await client.post(f"/tasks/{task_id}/cancel")
        assert resp.status_code == 200
        task = await wait_state(client, task_id, "canceled")
        assert task["state"] == "canceled"


async def test_handler_exception_marks_failed():
    server = make_server()

    @server.skill
    async def handle(_ctx):
        raise ValueError("boom")

    async with client_for(server) as client:
        task_id = await create_task(client)
        events = await collect_events(client, task_id)
        assert any(e == "error" and "boom" in d for e, d in events)
        task = (await client.get(f"/tasks/{task_id}")).json()
        assert task["state"] == "failed"
        assert "boom" in task["error"]


async def test_auth_required():
    server = make_server(auth_token="secret")

    @server.skill
    async def handle(_ctx):
        return "ok"

    async with client_for(server) as client:
        assert (await client.get("/card")).status_code == 401
        assert (await client.get("/health")).status_code == 200  # health 开放
        assert (await client.get("/card", headers=AUTH)).status_code == 200
        with pytest.raises(AssertionError):
            await create_task(client)  # 无 token 应为 401
        task_id = await create_task(client, headers=AUTH)
        assert (await client.get(f"/tasks/{task_id}", headers=AUTH)).status_code == 200


async def test_late_subscriber_gets_terminal_state():
    server = make_server()

    @server.skill
    async def handle(_ctx):
        return "done"

    async with client_for(server) as client:
        task_id = await create_task(client)
        await wait_state(client, task_id, "completed")
        events = await collect_events(client, task_id)  # 结束后才订阅
        assert events[0] == ("delta", '{"text": "done"}')  # 补发最终增量
        assert json.loads(events[-1][1])["state"] == "completed"


async def test_late_subscriber_replays_deltas():
    """竞态:任务在订阅 events 前已跑完,迟到的订阅者必须拿到完整增量流。"""

    server = make_server()

    @server.skill
    async def handle(ctx):
        await ctx.emit("进度 1 ")
        await ctx.emit("进度 2 ")
        return "结论"

    async with client_for(server) as client:
        task_id = await create_task(client)
        await wait_state(client, task_id, "completed")
        events = await collect_events(client, task_id)
        deltas = [json.loads(d)["text"] for e, d in events if e == "delta"]
        assert deltas == ["进度 1 ", "进度 2 ", "结论"]
        assert json.loads(events[-1][1])["state"] == "completed"


async def test_artifact_broadcast_persisted_and_replayed():
    server = make_server()

    @server.skill
    async def handle(ctx):
        await ctx.add_artifact("fix.diff", [TextPart(text="--- a\n+++ b")])
        return "done"

    async with client_for(server) as client:
        task_id = await create_task(client)
        # 实时订阅:收到 artifact 事件
        events = await collect_events(client, task_id)
        assert any(e == "artifact" and json.loads(d)["name"] == "fix.diff" for e, d in events)
        # 持久化:GET /tasks 能取回
        task = (await client.get(f"/tasks/{task_id}")).json()
        assert task["artifacts"][0]["parts"][0]["text"] == "--- a\n+++ b"
        # 迟到订阅:补发 artifact
        events = await collect_events(client, task_id)
        assert any(e == "artifact" and json.loads(d)["name"] == "fix.diff" for e, d in events)
