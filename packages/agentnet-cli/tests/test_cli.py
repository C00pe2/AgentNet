"""CLI 纯逻辑测试:card 加载、serve 模块加载。"""

import pytest
import typer
from agentnet_cli.main import load_card, load_server

CARD_YAML = """
agent_id: go-reviewer
name: Go Reviewer
description: Go 代码审查
natural_capabilities: 擅长 goroutine 泄漏、channel 死锁
capabilities:
  - go
  - code-review
endpoint: http://localhost:8001
auth:
  type: bearer
  token: my-token
"""


def test_load_card_valid(tmp_path):
    path = tmp_path / "card.yaml"
    path.write_text(CARD_YAML, encoding="utf-8")
    card = load_card(path)
    assert card.agent_id == "go-reviewer"
    assert card.capabilities == ["go", "code-review"]
    assert card.auth.token == "my-token"


def test_load_card_invalid(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("agent_id: 'bad id with spaces'\nname: x\n", encoding="utf-8")
    with pytest.raises(typer.BadParameter):
        load_card(path)


def test_load_server_ok(tmp_path):
    path = tmp_path / "my_agent.py"
    path.write_text(
        "from agentnet_core import AgentCard\n"
        "from agentnet_sdk import AgentServer\n"
        "server = AgentServer(AgentCard(agent_id='demo', name='Demo'))\n",
        encoding="utf-8",
    )
    server = load_server(path)
    assert server.card.agent_id == "demo"


def test_load_server_missing(tmp_path):
    path = tmp_path / "no_server.py"
    path.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(typer.BadParameter):
        load_server(path)
