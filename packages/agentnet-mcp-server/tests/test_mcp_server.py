"""MCP server 测试:fastmcp 内存客户端 + stub router,全程离线。"""

from dataclasses import dataclass

import pytest
from agentnet_core import AgentCard
from agentnet_mcp_server import create_server
from fastmcp import Client


@dataclass
class _Candidate:
    score: float
    card: AgentCard


@dataclass
class _AskResult:
    routed: bool
    fallback: bool
    agent_id: str | None
    answer: str


def _card(agent_id: str = "go-reviewer") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        name="Go Reviewer",
        description="Go 代码审查",
        capabilities=["go"],
    )


class StubRouter:
    """满足 Router 鸭子类型的测试替身。"""

    def __init__(self, ask_result: _AskResult | None = None) -> None:
        self.ask_result = ask_result or _AskResult(True, False, "go-reviewer", "发现并发 bug")

    async def search(self, query: str, top_k: int | None = None) -> list[_Candidate]:
        assert query and top_k
        return [_Candidate(0.88, _card())]

    async def list_agents(self) -> list[dict]:
        return [
            {
                "agent_id": "go-reviewer",
                "name": "Go Reviewer",
                "description": "Go 代码审查",
                "status": "active",
                "reputation": {"calls": 12, "success_rate": 0.92, "rating": 4.5},
            }
        ]

    async def ask(self, query: str) -> _AskResult:
        assert query
        return self.ask_result


@pytest.fixture
def server():
    return create_server(router=StubRouter())  # type: ignore[arg-type]


async def _tool_text(server, name: str, args: dict) -> str:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        return result.content[0].text  # type: ignore[union-attr]


async def test_tools_registered(server):
    async with Client(server) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"agentnet_search", "agentnet_list", "agentnet_ask"}


async def test_search_tool(server):
    text = await _tool_text(server, "agentnet_search", {"query": "review go 代码", "top_k": 5})
    assert "go-reviewer" in text and "0.88" in text


async def test_list_tool(server):
    text = await _tool_text(server, "agentnet_list", {})
    assert "go-reviewer" in text
    assert "92%" in text  # 成功率展示


async def test_ask_tool_routed_marks_untrusted(server):
    text = await _tool_text(server, "agentnet_ask", {"query": "并发问题"})
    assert "发现并发 bug" in text
    assert "go-reviewer" in text and "不受信" in text  # 远程内容必须标注不受信


async def test_ask_tool_fallback():
    server = create_server(router=StubRouter(_AskResult(False, True, None, "本地答案")))  # type: ignore[arg-type]
    text = await _tool_text(server, "agentnet_ask", {"query": "无人能答"})
    assert "本地答案" in text and "本地模型" in text
