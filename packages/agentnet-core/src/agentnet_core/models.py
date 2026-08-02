"""AgentNet Task Protocol 核心数据模型。

只有三个核心资源:Task / Message(+Part) / Artifact,外加 AgentCard。
所有模型只做数据结构定义,不含任何业务逻辑。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from .enums import MessageRole, TaskState

AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]{1,64}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Part:消息/工件的内容单元
# ---------------------------------------------------------------------------


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class FilePart(BaseModel):
    """文件内容:url 引用与 base64 内联二选一。"""

    type: Literal["file"] = "file"
    name: str
    mime_type: str = "application/octet-stream"
    url: str | None = None
    data: str | None = None  # base64


class DataPart(BaseModel):
    """任意结构化 JSON。"""

    type: Literal["data"] = "data"
    data: dict[str, Any] = Field(default_factory=dict)


Part = Annotated[TextPart | FilePart | DataPart, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# Message / Artifact / Task
# ---------------------------------------------------------------------------


class Message(BaseModel):
    role: MessageRole
    parts: list[Part]
    created_at: datetime = Field(default_factory=_utcnow)

    @classmethod
    def user_text(cls, text: str) -> Message:
        return cls(role=MessageRole.USER, parts=[TextPart(text=text)])

    @classmethod
    def agent_text(cls, text: str) -> Message:
        return cls(role=MessageRole.AGENT, parts=[TextPart(text=text)])

    def text_content(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))


class Artifact(BaseModel):
    """Task 的结构化产出(代码 diff、生成的文件、结构化数据等)。"""

    name: str
    parts: list[Part]
    created_at: datetime = Field(default_factory=_utcnow)


class Task(BaseModel):
    id: str
    agent_id: str = ""
    state: TaskState = TaskState.SUBMITTED
    messages: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    def last_agent_text(self) -> str:
        """最后一条 agent 消息的文本,没有则返回空串。"""
        for msg in reversed(self.messages):
            if msg.role == MessageRole.AGENT:
                return msg.text_content()
        return ""


class TaskCreate(BaseModel):
    """POST /tasks 请求体。"""

    message: Message
    context: dict[str, Any] = Field(default_factory=dict)


class MessageAppend(BaseModel):
    """POST /tasks/{id}/messages 请求体(回答澄清 / 多轮)。"""

    message: Message


# ---------------------------------------------------------------------------
# AgentCard:注册时提交的能力名片
# ---------------------------------------------------------------------------


class AgentAuth(BaseModel):
    """agent endpoint 的访问凭证。注册时提交,存 Registry,不下发给消费者。"""

    type: Literal["none", "bearer"] = "bearer"
    token: str | None = None


class AgentPricing(BaseModel):
    model: Literal["free", "per-call"] = "free"
    price: float | None = None
    currency: str = "CNY"


class Reputation(BaseModel):
    """信誉数据。由 Registry 根据 Gateway 调用记录维护,注册方不可自填。"""

    calls: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    rating: float | None = None  # 消费者评分均值,0~5


class AgentCard(BaseModel):
    agent_id: str
    name: str
    description: str = ""
    natural_capabilities: str = ""  # 自然语言能力描述,Registry 为其计算 embedding
    capabilities: list[str] = Field(default_factory=list)  # 结构化标签
    # agent 服务 base url,如 http://host:8001。
    # 注册时必填(Registry 校验);对消费者下发时由 Registry 掩码为空(Gateway 模式不暴露真实地址)。
    endpoint: str = ""
    auth: AgentAuth = Field(default_factory=AgentAuth)
    pricing: AgentPricing = Field(default_factory=AgentPricing)
    version: str = "0.1.0"
    provider: str | None = None  # 注册时由 Registry 填入 provider 标识
    reputation: Reputation | None = None  # 查询时由 Registry 填入

    @field_validator("agent_id")
    @classmethod
    def _validate_agent_id(cls, v: str) -> str:
        if not AGENT_ID_PATTERN.match(v):
            raise ValueError("agent_id 只能包含字母/数字/_/./-,长度 1~64")
        return v

    @field_validator("endpoint")
    @classmethod
    def _normalize_endpoint(cls, v: str) -> str:
        v = v.rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("endpoint 必须是 http(s) URL")
        return v
