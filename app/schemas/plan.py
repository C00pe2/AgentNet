"""Plan Execution schema (SPEC §4.1 状态机)。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PlanStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class StepResult(BaseModel):
    step_id: int
    agent_id: str
    code: int
    content: str
    latency_ms: int
    status: str


class PlanExecutionRead(BaseModel):
    plan_id: int
    session_id: str
    raw_query: str
    execution_graph: dict[str, Any]
    status: PlanStatus
    created_at: datetime
    results: list[StepResult] = Field(default_factory=list)
