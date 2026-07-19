"""Session 相关 schema (SPEC §4 表 agent_sessions)。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SessionCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    user_id: str = Field(min_length=1, max_length=50)
    caller_dept: str = Field(min_length=1, max_length=100)


class SessionRead(SessionCreate):
    created_at: datetime
