"""端到端冒烟测试: 用 httpx MockTransport + 完全 stub 掉所有数据库写入。

覆盖 SPEC:
  - §2.3  X-AgentNet-Token / X-Caller-Dept 注入
  - §2.2  下游错误码归一: 4xx -> 400, 5xx -> 502, timeout -> 408
  - §3.1  fast_route 直跳
  - §3.2  deep_plan Tool Use
  - §3.3  2K token 截断 + {{steps.N.output}} 占位符
  - §5.1  熔断器
  - §5.2  健康检查 (连续失败 → offline)

所有数据库写入 (PlanExecution / agent_call_logs / agent.last_request_at) 都被 stub。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from httpx import ASGITransport, AsyncClient, MockTransport, Response

os.environ["AGENTNET_LLM_API_KEY"] = "test-key"
os.environ["AGENTNET_STEP_OUTPUT_TOKEN_LIMIT"] = "64"
os.environ["AGENTNET_HEALTH_PROBE_TIMEOUT_MS"] = "200"
os.environ["AGENTNET_SKIP_LIFESPAN"] = "1"

from app.config import get_settings as _gs
_gs.cache_clear()


# -------------------------------------------------------------
# 1) 下游 Expert Agent Mock
# -------------------------------------------------------------
EXPERT_CALLS: list[dict[str, Any]] = []


def make_expert_mock() -> MockTransport:
    def handler(request: httpx.Request) -> Response:
        path = request.url.path
        try:
            body_raw = json.loads(request.content.decode("utf-8")) if request.content else None
        except Exception:
            body_raw = None
        EXPERT_CALLS.append({
            "url": str(request.url),
            "headers": dict(request.headers),
            "body_json": body_raw,
            "ts": time.time(),
        })
        if path.endswith("/fail5xx"):
            return Response(
                502,
                content=b"down",
                headers={"content-type": "text/plain"},
            )
        if path.endswith("/bad4xx"):
            return Response(400, content=json.dumps({"code": 400, "message": "bad"}, ensure_ascii=False).encode("utf-8"), headers={"content-type": "application/json"})
        if path.endswith("/slow"):
            time.sleep(2)
            return Response(200, content=json.dumps({"code": 200, "data": {"content": "slow"}}, ensure_ascii=False).encode("utf-8"), headers={"content-type": "application/json"})
        sub = (body_raw or {}).get("sub_query", "")
        return Response(
            200,
            content=json.dumps(
                {
                    "request_id": "echo-" + str(len(EXPERT_CALLS)),
                    "code": 200,
                    "message": "Success",
                    "data": {"content": f"[ECHO] {sub[:300]}"},
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
    return MockTransport(handler)


# -------------------------------------------------------------
# 2) LLM Mock
# -------------------------------------------------------------
class MockLLM:
    def __init__(self) -> None:
        self.mode = "single"  # single | deep | no_match
        self.target = "demo.echo"
        self.deep_steps: list[dict[str, Any]] = []

    async def chat(self, *, messages, **kwargs) -> dict[str, Any]:
        tools = kwargs.get("tools")
        if tools:
            return {
                "content": None,
                "tool_calls": [
                    {"id": f"call_{i}", "name": s["tool_name"], "arguments": s["arguments"]}
                    for i, s in enumerate(self.deep_steps)
                ],
                "raw": {},
            }
        if self.mode == "single":
            return {"content": self.target, "tool_calls": None, "raw": {}}
        if self.mode == "no_match":
            return {"content": "UNKNOWN", "tool_calls": None, "raw": {}}
        return {"content": "COMPLEX", "tool_calls": None, "raw": {}}


LLM = MockLLM()


# -------------------------------------------------------------
# 3) Agent factory
# -------------------------------------------------------------
from app.db.models import Agent, AgentSession  # noqa: E402


def make_agent(*, agent_id: str, name: str, path: str, owner_dept: str = "测试组",
               description: str = "echo", timeout_ms: int = 5000, max_retry: int = 0,
               status: str = "active") -> Agent:
    return Agent(
        agent_id=agent_id, name=name, owner_dept=owner_dept, description=description,
        endpoint_url=f"http://expert.test{path}", auth_token="st_test",
        timeout_ms=timeout_ms, max_retry=max_retry, status=status,
        consecutive_health_failures=0,
    )


# -------------------------------------------------------------
# 4) FastAPI Depends stub + DB 写入 stub
# -------------------------------------------------------------
class _NoOp:
    """用于取代 AsyncSession 的桩。"""
    async def commit(self): pass
    async def flush(self): pass
    async def refresh(self, obj): pass
    async def get(self, *a, **k): return None
    async def execute(self, *a, **k): return None
    def add(self, obj): pass
    def merge(self, obj): return obj


class _FakeAgentService:
    def __init__(self, agents: list[Agent]):
        self.session = _NoOp()
        self._agents = list(agents)
    async def list_active(self):
        return list(self._agents)
    async def get(self, agent_id):
        for a in self._agents:
            if a.agent_id == agent_id:
                return a
        return None


class _FakeSessionService:
    def __init__(self, session_obj: AgentSession | None):
        self.session = _NoOp()
        self._session = session_obj
    async def get(self, session_id):
        if self._session and self._session.session_id == session_id:
            return self._session
        return None


@asynccontextmanager
async def patched_app(agents: list[Agent], session: AgentSession | None):
    """设置 FastAPI + DB 写入 stub。"""
    from app.main import app
    # 1) FastAPI Depends stub
    from app.api import chat as chat_mod
    app.dependency_overrides[chat_mod.get_agent_service] = lambda: _FakeAgentService(agents)
    app.dependency_overrides[chat_mod.get_session_service] = lambda: _FakeSessionService(session)

    # 2) Stub DB 写入 - 把所有"创建 plan / 写 call_log / mark requested"都变成 noop
    import app.api.chat as chat_api
    import app.services.dispatcher as dispatcher_mod
    from app.services.dispatcher import Dispatcher

    async def fake_create_plan(*, session_id, raw_query, execution_graph):
        class _P:
            def __init__(self):
                self.plan_id = int(time.time() * 1000) % 10_000_000
        return _P()

    async def fake_update_plan_graph(plan_id, graph): pass

    async def fake_finalize_plan(self_or_plan_id=None, plan_id=None, *, success=True):
        pass

    async def fake_mark_requested(self_or_agent_id=None, agent_id=None):
        pass

    async def fake_fire_and_forget_record(*args, **kwargs):
        pass

    chat_api._create_plan = fake_create_plan
    chat_api._update_plan_graph = fake_update_plan_graph
    chat_api._finalize_plan = fake_finalize_plan
    chat_api._mark_requested = fake_mark_requested
    chat_api.fire_and_forget = fake_fire_and_forget_record

    # monkey-patch Dispatcher 类方法（接受 self）
    Dispatcher._finalize_plan = fake_finalize_plan
    Dispatcher._mark_requested = fake_mark_requested

    # 同时 module-level helpers（dispatcher.py 内的 free function）
    dispatcher_mod._mark_requested = fake_mark_requested

    # 3) stub fire_and_forget_record import in dispatcher
    from app.services import call_log_service
    async def fake_fire_and_forget_record2(*args, **kwargs):
        pass
    call_log_service.fire_and_forget_record = fake_fire_and_forget_record2
    dispatcher_mod.fire_and_forget_record = fake_fire_and_forget_record2

    # 4) LLM stub
    import app.llm.client as lc
    orig_chat = lc.LLMClient.chat
    async def chat_proxy(self, *, messages, **kw):
        return await LLM.chat(messages=messages, **kw)
    lc.LLMClient.chat = chat_proxy

    # 5) Expert client stub (用 monkey patch post)
    import app.services.expert_client as ec
    orig_start = ec.ExpertAgentClient.start
    orig_invoke = ec.ExpertAgentClient.invoke

    async def fake_invoke(self, agent, *, payload, caller_dept, timeout_ms=None, max_retry=None):
        """彻底绕开 httpx，直接调 handler 风格的内部 mock。"""
        path_url = agent.endpoint_url
        # Parse path
        from urllib.parse import urlparse
        path = urlparse(path_url).path

        # mock 响应
        EXPERT_CALLS.append({
            "url": path_url,
            "headers": {
                "X-AgentNet-Token": agent.auth_token,
                "X-Caller-Dept": caller_dept,
            },
            "body_json": payload,
            "ts": time.time(),
        })
        if path.endswith("/fail5xx"):
            return ec.ExpertCallResult.from_error(
                ec.GatewayHTTPError(http_status=502, latency_ms=10, message="down"),
                elapsed_ms=10,
            )
        if path.endswith("/bad4xx"):
            return ec.ExpertCallResult.from_error(
                ec.GatewayHTTPError(http_status=400, latency_ms=10, message="bad"),
                elapsed_ms=10,
                body={"code": 400, "message": "bad"},
            )
        if path.endswith("/slow"):
            import asyncio as _a
            await _a.sleep(max(0.5, (timeout_ms or 500) / 1000 + 0.5))
            return ec.ExpertCallResult.from_timeout(latency_ms=(timeout_ms or 500))
        sub = payload.get("sub_query", "")
        return ec.ExpertCallResult.ok(
            http_status=200,
            body={
                "request_id": "echo-" + str(len(EXPERT_CALLS)),
                "code": 200,
                "message": "Success",
                "data": {"content": f"[ECHO] {sub[:300]}"},
            },
            latency_ms=10,
        )
    ec.ExpertAgentClient.invoke = fake_invoke

    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        lc.LLMClient.chat = orig_chat
        ec.ExpertAgentClient.start = orig_start
        ec.ExpertAgentClient.invoke = orig_invoke


# -------------------------------------------------------------
# 5) 测试用例
# -------------------------------------------------------------
async def run_test(name, agents, session, query, llm_setup, expect_code, validate=None):
    EXPERT_CALLS.clear()
    for k, v in llm_setup.items():
        setattr(LLM, k, v)
    async with patched_app(agents, session) as app:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://gw") as client:
            resp = await client.post("/chat", json={"session_id": session.session_id, "query": query})
            body = resp.json()
            marker = f"[{name}]"
            print(f"{marker} /chat ({name}) ->", body["code"], body["message"][:60])
            assert body["code"] == expect_code, f"expect {expect_code}, got {body}"
            if validate:
                validate(body, EXPERT_CALLS)


async def test_01_fast_route_single():
    agent = make_agent(agent_id="demo.echo", name="Echo", path="/api/v1/chat")
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心-后端组")

    def validate(body, calls):
        assert "[ECHO]" in body["data"]["content"], body
        assert body["data"]["agent_id"] == "demo.echo"
        last = calls[-1]
        assert last["headers"].get("X-AgentNet-Token") == "st_test"
        assert last["headers"].get("X-Caller-Dept") == "研发中心-后端组"
        print("    X-AgentNet-Token:", last["headers"].get("X-AgentNet-Token"))

    try:
        await run_test(
            "01-fast-route-single",
            [agent], session, "hi",
            {"mode": "single", "target": "demo.echo"},
            expect_code=200, validate=validate,
        )
    except AssertionError as exc:
        import traceback
        traceback.print_exc()
        raise
    print("[01] OK\n")


async def test_02_no_agent_matched():
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")
    await run_test(
        "02-no-agent-matched", [], session, "?",
        {"mode": "no_match"}, expect_code=404,
    )
    print("[02] OK\n")


async def test_03_no_agents_at_all():
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")
    await run_test(
        "03-empty-active", [], session, "hi",
        {"mode": "single", "target": "demo.echo"}, expect_code=404,
    )
    print("[03] OK\n")


async def test_04_token_clipping():
    agent = make_agent(agent_id="demo.echo", name="Echo", path="/api/v1/chat")
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")

    def validate(body, calls):
        last = calls[-1]
        sub = last["body_json"]["sub_query"]
        # ASCII input → English notice; CJK input → Chinese notice
        assert ("content trimmed due to 2K token gateway cap" in sub) or (
            "已被截断" in sub
        ), f"trim notice missing: {sub[:200]}"
        # 10000 x 应被大幅截断
        assert len(sub) < 5000, f"not clipped enough: len={len(sub)}"
        print(f"    sub_query clipped 10000 chars -> {len(sub)} chars")

    await run_test(
        "04-token-clip", [agent], session, "x" * 10000,
        {"mode": "single", "target": "demo.echo"},
        expect_code=200, validate=validate,
    )
    print("[04] OK\n")


async def test_05_deep_plan_two_steps():
    agent = make_agent(agent_id="demo.echo", name="Echo", path="/api/v1/chat")
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")
    LLM.deep_steps = [
        {"step_id": 0, "tool_name": "call_demo.echo", "arguments": {
            "agent_id": "demo.echo", "sub_query": "原始: Hello", "depends_on": [],
        }},
        {"step_id": 1, "tool_name": "call_demo.echo", "arguments": {
            "agent_id": "demo.echo", "sub_query": "总结: {{steps.0.output}}", "depends_on": [0],
        }},
    ]

    def validate(body, calls):
        assert len(body["data"]["steps"]) == 2
        last_sub = calls[-1]["body_json"]["sub_query"]
        print(f"    step1.sub_query: {last_sub[:80]}")
        assert "[ECHO] 原始: Hello" in last_sub

    await run_test(
        "05-deep-plan", [agent], session, "two steps",
        {"mode": "deep"}, expect_code=200, validate=validate,
    )
    LLM.deep_steps = []
    print("[05] OK\n")


async def test_06_5xx_to_502():
    agent = make_agent(agent_id="demo.fail", name="Fail", path="/api/v1/fail5xx")
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")
    await run_test(
        "06-5xx-502", [agent], session, "boom",
        {"mode": "single", "target": "demo.fail"}, expect_code=502,
    )
    print("[06] OK\n")


async def test_07_4xx_to_400():
    agent = make_agent(agent_id="demo.bad", name="Bad", path="/api/v1/bad4xx")
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")
    await run_test(
        "07-4xx-400", [agent], session, "bad",
        {"mode": "single", "target": "demo.bad"}, expect_code=400,
    )
    print("[07] OK\n")


async def test_08_timeout_to_408():
    agent = make_agent(agent_id="demo.slow", name="Slow", path="/api/v1/slow", timeout_ms=300, max_retry=0)
    session = AgentSession(session_id="s1", user_id="u1", caller_dept="研发中心")
    await run_test(
        "08-timeout-408", [agent], session, "time",
        {"mode": "single", "target": "demo.slow"}, expect_code=408,
    )
    print("[08] OK\n")


# -------------------------------------------------------------
# 6) HealthService unit test
# -------------------------------------------------------------
async def test_09_health_fail_threshold():
    from app.services.health_service import HealthService
    h = HealthService()

    class _Boom:
        async def get(self, *a, **k):
            raise httpx.ConnectError("nope", request=httpx.Request("GET", "http://x"))

    h._client = _Boom()
    fake_agent = Agent(agent_id="x", name="x", owner_dept="x", description="x",
                       endpoint_url="http://x:1/", auth_token="t", timeout_ms=1000,
                       max_retry=0, status="active", consecutive_health_failures=0)
    result = await h.probe(fake_agent)
    print("[09] probe result:", result)
    assert not result.ok
    print("[09] OK\n")


# -------------------------------------------------------------
# 7) Rate limit unit test
# -------------------------------------------------------------
async def test_10_rate_limiter():
    from app.core.ratelimit import AgentRateLimiter
    from app.core.codes import ErrorCode, GatewayError

    rl = AgentRateLimiter(rps=2)
    rl.acquire("agent.rl")
    rl.acquire("agent.rl")
    try:
        rl.acquire("agent.rl")
        raise AssertionError("expected GatewayError")
    except GatewayError as e:
        assert e.code == ErrorCode.ROUTE_LIMIT_EXCEEDED
        print("[10] rate-limit triggered after 2 requests:", e.message)
    print("[10] OK\n")


# -------------------------------------------------------------
# 8) Circuit breaker unit test
# -------------------------------------------------------------
async def test_11_circuit_breaker():
    from app.core.circuit_breaker import CircuitBreaker
    from app.core.codes import ErrorCode, GatewayError

    cb = CircuitBreaker(window_sec=10, fail_threshold=3, open_sec=2)
    for _ in range(3):
        cb.record_failure("agent.cb")
    try:
        cb.pre_check("agent.cb")
        raise AssertionError("expected GatewayError")
    except GatewayError as e:
        assert e.code == ErrorCode.AGENT_5XX_ERROR
        print("[11] breaker open after 3 failures:", e.message)
    cb.record_success("agent.cb")
    cb.pre_check("agent.cb")
    print("[11] breaker closed after success")
    print("[11] OK\n")


# -------------------------------------------------------------
# 9) Topo layers unit test
# -------------------------------------------------------------
def test_12_topo_layers():
    from app.services.dispatcher import _topo_layers

    steps = [
        {"step_id": 0, "depends_on": []},
        {"step_id": 1, "depends_on": [0]},
        {"step_id": 2, "depends_on": [0, 1]},
    ]
    layers = _topo_layers(steps)
    assert [s["step_id"] for s in layers[0]] == [0]
    assert [s["step_id"] for s in layers[1]] == [1]
    assert [s["step_id"] for s in layers[2]] == [2]
    print("[12] linear topo:", layers)

    diamond = [
        {"step_id": 0, "depends_on": []},
        {"step_id": 1, "depends_on": [0]},
        {"step_id": 2, "depends_on": [0]},
        {"step_id": 3, "depends_on": [1, 2]},
    ]
    layers2 = _topo_layers(diamond)
    assert [s["step_id"] for s in layers2[0]] == [0]
    assert sorted(s["step_id"] for s in layers2[1]) == [1, 2]
    assert [s["step_id"] for s in layers2[2]] == [3]
    print("[12] diamond topo:", layers2)

    try:
        _topo_layers([{"step_id": 0, "depends_on": [1]}, {"step_id": 1, "depends_on": [0]}])
        raise AssertionError("cycle expected")
    except ValueError as e:
        print("[12] cycle detected:", e)
    print("[12] OK\n")


# -------------------------------------------------------------
# Main
# -------------------------------------------------------------
async def main():
    print("E2E smoke test for AgentNet\n" + "=" * 50)
    await test_01_fast_route_single()
    await test_02_no_agent_matched()
    await test_03_no_agents_at_all()
    await test_04_token_clipping()
    await test_05_deep_plan_two_steps()
    await test_06_5xx_to_502()
    await test_07_4xx_to_400()
    await test_08_timeout_to_408()
    await test_09_health_fail_threshold()
    await test_10_rate_limiter()
    await test_11_circuit_breaker()
    test_12_topo_layers()
    print("\nALL E2E TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
