"""度量相关 schema (SPEC §6)。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CrossDeptLinkingPair(BaseModel):
    caller_dept: str
    owner_dept: str
    link_count: int


class CrossDeptLinkingPairsResponse(BaseModel):
    count: int
    pairs: list[CrossDeptLinkingPair]


class ReusabilityRow(BaseModel):
    agent_id: str
    name: str
    unique_caller_depts: int
    total_calls: int
    cross_dept_calls: int
    cross_dept_saturation_pct: float = Field(
        description="非本部门调用 / 全局调用 * 100%",
    )


class ReusabilityDepthResponse(BaseModel):
    items: list[ReusabilityRow]
