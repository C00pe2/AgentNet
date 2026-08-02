"""Router 测试:召回→精排→调用→兜底 全链路(registry 与 LLM 均 mock)。"""

import json

import httpx
import pytest
from agentnet_router import (
    LLMSettings,
    RegistryClient,
    Router,
    RouterSettings,
    find_secret,
    parse_rerank_response,
    redact_pii,
)
from agentnet_router.llm import RERANK_SYSTEM

GO_CARD = {
    "agent_id": "go-reviewer",
    "name": "Go Reviewer",
    "description": "Go 审查",
    "natural_capabilities": "擅长 Go 并发 bug",
    "capabilities": ["go"],
    "endpoint": "",
    "auth": {"type": "none"},
}
TRANSLATOR_CARD = {**GO_CARD, "agent_id": "translator", "name": "Translator"}

SSE_OK = (
    'event: state\ndata: {"state": "working"}\n\n'
    'event: delta\ndata: {"text": "发现 "}\n\n'
    'event: delta\ndata: {"text": "并发 bug"}\n\n'
    'event: artifact\ndata: {"name": "report.md", "parts": [{"type": "text", "text": "# 报告"}]}\n\n'
    'event: state\ndata: {"state": "completed"}\n\n'
)

SSE_ASK = (
    'event: state\ndata: {"state": "working"}\n\n'
    'event: message\ndata: {"role": "agent", "parts": [{"type": "text", "text": "哪个分支?"}]}\n\n'
    'event: state\ndata: {"state": "input-required"}\n\n'
    'event: state\ndata: {"state": "working"}\n\n'
    'event: delta\ndata: {"text": "main 已审查"}\n\n'
    'event: state\ndata: {"state": "completed"}\n\n'
)

SSE_FAILED = 'event: state\ndata: {"state": "working"}\n\nevent: error\ndata: {"error": "boom"}\n\n'


def make_llm_handler(rerank_json: str | None = None, fallback_text: str = "本地 LLM 答案"):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        content = rerank_json if system == RERANK_SYSTEM else fallback_text
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def make_registry_handler(
    candidates: list[dict] | None = None,
    sse: str = SSE_OK,
    task_id: str = "t-1",
    posted_messages: list | None = None,
    canceled: list | None = None,
    created_tasks: list | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents/search":
            cards = candidates if candidates is not None else [GO_CARD, TRANSLATOR_CARD]
            data = {
                "query": "q",
                "candidates": [
                    {"score": 0.9 - i * 0.3, "card": c} for i, c in enumerate(cards)
                ],
            }
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": data})
        if path.endswith("/tasks") and request.method == "POST":
            if created_tasks is not None:
                created_tasks.append(json.loads(request.content))
            data = {"id": task_id, "state": "submitted", "messages": [], "artifacts": []}
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": data})
        if path.endswith("/events"):
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        if path.endswith("/messages") and request.method == "POST":
            if posted_messages is not None:
                posted_messages.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {}})
        if path.endswith("/cancel") and request.method == "POST":
            if canceled is not None:
                canceled.append(task_id)
            return httpx.Response(200, json={"code": 0, "message": "ok", "data": {}})
        if "/tasks/" in path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "id": task_id,
                        "state": "completed",
                        "messages": [{"role": "agent", "parts": [{"type": "text", "text": "最终消息"}]}],
                        "artifacts": [],
                    },
                },
            )
        return httpx.Response(404, json={"code": 404, "message": "not found", "data": None})

    return handler


def make_router(
    registry_handler,
    llm_handler,
    threshold: float = 0.65,
    redact_pii: bool = False,
    block_secrets: bool = True,
) -> Router:
    settings = RouterSettings(
        registry_url="http://registry.test",
        consumer_key="ck",
        llm=LLMSettings(base_url="http://llm.test/v1", api_key="k", model="m"),
        threshold=threshold,
        redact_pii=redact_pii,
        block_secrets=block_secrets,
    )
    client = RegistryClient(
        "http://registry.test", "ck", transport=httpx.MockTransport(registry_handler)
    )
    return Router(
        settings,
        client=client,
        llm_http=httpx.AsyncClient(transport=httpx.MockTransport(llm_handler)),
    )


RERANK_PICK_GO = '{"agent_id": "go-reviewer", "confidence": 0.92, "reason": "专业匹配"}'
RERANK_NULL = '{"agent_id": null, "confidence": 0.8, "reason": "都不合适"}'


async def test_ask_routed_success():
    router = make_router(make_registry_handler(), make_llm_handler(RERANK_PICK_GO))
    deltas: list[str] = []
    result = await router.ask("帮我 review 这段 go 代码", on_delta=deltas.append)
    assert result.routed is True
    assert result.fallback is False
    assert result.agent_id == "go-reviewer"
    assert result.answer == "发现 并发 bug"
    assert deltas == ["发现 ", "并发 bug"]
    assert result.artifacts[0].name == "report.md"
    assert result.task_id == "t-1"


async def test_ask_no_candidates_fallback():
    router = make_router(
        make_registry_handler(candidates=[]), make_llm_handler(RERANK_PICK_GO)
    )
    result = await router.ask("你好")
    assert result.fallback is True
    assert result.routed is False
    assert result.answer == "本地 LLM 答案"


async def test_ask_rerank_null_fallback():
    router = make_router(make_registry_handler(), make_llm_handler(RERANK_NULL))
    result = await router.ask("帮我修电脑")
    assert result.fallback is True
    assert "无合适" in result.reason


async def test_ask_low_confidence_fallback():
    router = make_router(
        make_registry_handler(),
        make_llm_handler('{"agent_id": "go-reviewer", "confidence": 0.3, "reason": "勉强"}'),
    )
    result = await router.ask("go?")
    assert result.fallback is True
    assert "低于阈值" in result.reason


async def test_ask_unknown_agent_id_fallback():
    """LLM 幻觉返回不存在的 agent_id 时必须拒绝。"""
    router = make_router(
        make_registry_handler(),
        make_llm_handler('{"agent_id": "ghost", "confidence": 0.99, "reason": "幻觉"}'),
    )
    result = await router.ask("go?")
    assert result.fallback is True
    assert "未知 agent_id" in result.reason


async def test_ask_task_failed_fallback():
    router = make_router(
        make_registry_handler(sse=SSE_FAILED), make_llm_handler(RERANK_PICK_GO)
    )
    result = await router.ask("帮我 review")
    assert result.fallback is True
    assert result.agent_id == "go-reviewer"  # 路由过,但失败了
    assert result.answer == "本地 LLM 答案"


async def test_ask_input_required_flow():
    posted: list = []
    router = make_router(
        make_registry_handler(sse=SSE_ASK, posted_messages=posted),
        make_llm_handler(RERANK_PICK_GO),
    )

    async def answer(question: str) -> str:
        assert question == "哪个分支?"
        return "main"

    result = await router.ask("review 一下", on_input_required=answer)
    assert result.fallback is False
    assert result.answer == "main 已审查"
    assert len(posted) == 1
    assert posted[0]["message"]["parts"][0]["text"] == "main"


async def test_ask_input_required_without_callback_cancels():
    """agent 要求澄清但消费端无应答回调:主动取消任务并本地兜底,不能挂起。"""
    canceled: list = []
    router = make_router(
        make_registry_handler(sse=SSE_ASK, canceled=canceled),
        make_llm_handler(RERANK_PICK_GO),
    )
    result = await router.ask("review 一下")
    assert result.fallback is True
    assert result.answer == "本地 LLM 答案"
    assert "未提供应答回调" in result.reason
    assert canceled == ["t-1"]  # 任务被主动取消


async def test_ask_query_with_secret_never_leaves():
    """含密钥的 query 按安全策略不外发:直接本地兜底,registry 一个请求都不收。"""
    touched: list = []

    def spy_handler(request: httpx.Request) -> httpx.Response:
        touched.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {}})

    router = make_router(spy_handler, make_llm_handler(RERANK_PICK_GO))
    result = await router.ask("我的 key 是 sk-a1b2c3d4e5f6g7h8i9j0k1l2m3n4,帮我 review 代码")
    assert result.fallback is True
    assert "密钥" in result.reason and "安全策略" in result.reason
    assert result.answer == "本地 LLM 答案"  # 本地 LLM 是用户自己配置的端点,可收原文
    assert touched == []  # registry 零接触


async def test_ask_redacts_pii_before_outbound():
    """开启 redact_pii 后,外发给网络的 query 已脱敏。"""
    created: list = []
    router = make_router(
        make_registry_handler(created_tasks=created),
        make_llm_handler(RERANK_PICK_GO),
        redact_pii=True,
    )
    result = await router.ask("我的邮箱是 bob@example.com 手机号 13812345678,帮我 review go 代码")
    assert result.routed is True
    sent = created[0]["message"]["parts"][0]["text"]
    assert "bob@example.com" not in sent and "13812345678" not in sent
    assert "[邮箱]" in sent and "[手机号]" in sent


def test_find_secret_patterns():
    assert find_secret("普通问题") is None
    assert find_secret("sk-a1b2c3d4e5f6g7h8i9j0") == "OpenAI/兼容 API key"
    assert find_secret("AKIAIOSFODNN7EXAMPLE") == "AWS Access Key"
    assert "私钥" in (find_secret("-----BEGIN RSA PRIVATE KEY-----") or "")
    assert find_secret("password: hunter2secret") == "密码赋值"


def test_redact_pii_patterns():
    text = redact_pii("联系 bob@example.com 或 13812345678,身份证 11010119900307123X")
    assert "bob@example.com" not in text and "[邮箱]" in text
    assert "13812345678" not in text and "[手机号]" in text
    assert "11010119900307123X" not in text and "[身份证号]" in text
    assert redact_pii("没有敏感信息") == "没有敏感信息"


async def test_route_decision_reason_when_registry_down():
    def down_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    router = make_router(down_handler, make_llm_handler(RERANK_PICK_GO))
    decision = await router.route("任何")
    assert decision.routed is False
    assert "registry 不可用" in decision.reason


def test_parse_rerank_response_variants():
    r = parse_rerank_response('{"agent_id": "a", "confidence": 0.5, "reason": "x"}')
    assert (r.agent_id, r.confidence) == ("a", 0.5)
    r = parse_rerank_response('```json\n{"agent_id": null, "confidence": 0.7, "reason": "y"}\n```')
    assert r.agent_id is None and r.confidence == 0.7
    r = parse_rerank_response('我认为:{"agent_id": "b", "confidence": 0.6, "reason": "z"} 如上')
    assert r.agent_id == "b"
    with pytest.raises(ValueError):
        parse_rerank_response("完全不是 JSON")
