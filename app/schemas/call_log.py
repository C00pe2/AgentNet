"""agent_call_logs 读取 schema。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CallLogRead(BaseModel):
    log_id: int
    plan_id: int | None
    session_id: str
    target_agent_id: str
    step_id: int
    request_snapshot: dict[str, Any]
    response_snapshot: dict[str, Any] | None
    code: int
    latency_ms: int
    status: str
    created_at: datetime
