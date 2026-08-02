"""agentnet-core 模型单元测试:序列化 round-trip、校验、状态机语义。"""

import pytest
from pydantic import ValidationError

from agentnet_core import (
    AgentCard,
    Artifact,
    DataPart,
    FilePart,
    Message,
    MessageAppend,
    MessageRole,
    Task,
    TaskCreate,
    TaskState,
    TextPart,
)


def test_part_discriminator_roundtrip():
    task = Task(
        id="t1",
        agent_id="demo",
        messages=[
            Message.user_text("帮我 review 这段代码"),
            Message(
                role=MessageRole.AGENT,
                parts=[
                    TextPart(text="发现一个问题"),
                    DataPart(data={"severity": "high"}),
                    FilePart(name="fix.diff", mime_type="text/x-diff", data="ZjA="),
                ],
            ),
        ],
        artifacts=[Artifact(name="review.md", parts=[TextPart(text="# 报告")])],
    )
    dumped = task.model_dump(mode="json")
    restored = Task.model_validate(dumped)

    assert restored.messages[0].parts[0].text == "帮我 review 这段代码"
    assert isinstance(restored.messages[1].parts[1], DataPart)
    assert isinstance(restored.messages[1].parts[2], FilePart)
    assert restored.artifacts[0].parts[0].text == "# 报告"


def test_task_default_state_and_terminal():
    task = Task(id="t1")
    assert task.state == TaskState.SUBMITTED
    assert not task.is_terminal

    for s in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED):
        assert Task(id="t", state=s).is_terminal
    for s in (TaskState.SUBMITTED, TaskState.WORKING, TaskState.INPUT_REQUIRED):
        assert not Task(id="t", state=s).is_terminal


def test_task_state_wire_format():
    """状态值在 wire 上必须是协议定义的字符串(注意 input-required 是连字符)。"""
    assert TaskState.INPUT_REQUIRED.value == "input-required"
    task = Task(id="t1", state=TaskState.INPUT_REQUIRED)
    assert task.model_dump(mode="json")["state"] == "input-required"


def test_last_agent_text():
    task = Task(
        id="t1",
        messages=[Message.user_text("q"), Message.agent_text("a1"), Message.agent_text("a2")],
    )
    assert task.last_agent_text() == "a2"
    assert Task(id="t2").last_agent_text() == ""


def test_task_create_and_append():
    create = TaskCreate.model_validate(
        {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}, "context": {"k": 1}}
    )
    assert create.message.role == MessageRole.USER
    assert create.context == {"k": 1}

    append = MessageAppend(message=Message.user_text("补充"))
    assert append.message.text_content() == "补充"


def test_agent_card_valid():
    card = AgentCard(
        agent_id="alice.go-reviewer",
        name="Go Reviewer",
        description="Go 代码审查",
        natural_capabilities="擅长 goroutine 泄漏、channel 死锁",
        capabilities=["go", "code-review"],
        endpoint="http://localhost:8001/",  # 尾部斜杠会被归一化
    )
    assert card.endpoint == "http://localhost:8001"
    assert card.auth.type == "bearer"
    assert card.pricing.model == "free"
    assert card.reputation is None


@pytest.mark.parametrize("bad_id", ["", "has space", "中文id", "a" * 65, "id/with/slash"])
def test_agent_card_bad_id(bad_id):
    with pytest.raises(ValidationError):
        AgentCard(agent_id=bad_id, name="x", endpoint="http://localhost:8001")


def test_agent_card_bad_endpoint():
    with pytest.raises(ValidationError):
        AgentCard(agent_id="ok", name="x", endpoint="ftp://localhost")
