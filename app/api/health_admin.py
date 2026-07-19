"""健康检查 / 调度状态查询 API。"""
from __future__ import annotations

from fastapi import APIRouter

from app.core.circuit_breaker import get_circuit_breaker
from app.schemas.response import success_envelope

router = APIRouter(prefix="/admin/health", tags=["admin"])


@router.get("/circuit-breaker")
async def circuit_breaker_snapshot() -> dict:
    return success_envelope(get_circuit_breaker().snapshot())
