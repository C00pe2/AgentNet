"""Agent Manifest / 注册相关 schema (SPEC §2.1)。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

AgentIdStr = Annotated[
    str, StringConstraints(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.\-]+$")
]


class EndpointSpec(BaseModel):
    url: HttpUrl
    x_agentnet_token: str = Field(min_length=1, max_length=255)


class SlaSpec(BaseModel):
    timeout_ms: int = Field(default=15000, ge=1, le=600_000)
    max_retry: int = Field(default=1, ge=0, le=5)


class AgentManifest(BaseModel):
    """业务方提供的专家 Agent 注册元数据，对应 SPEC §2.1 准入协议。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: AgentIdStr
    name: str = Field(min_length=1, max_length=100)
    owner_department: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    endpoint: EndpointSpec
    sla: SlaSpec = Field(default_factory=SlaSpec)


class AgentCreate(AgentManifest):
    """REST 接收的入参。"""


class AgentRead(BaseModel):
    agent_id: AgentIdStr
    name: str
    owner_department: str
    description: str
    endpoint_url: str
    timeout_ms: int
    max_retry: int
    status: str
    created_at: datetime
