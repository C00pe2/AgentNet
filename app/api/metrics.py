"""度量 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import get_metrics_service
from app.schemas.response import success_envelope
from app.schemas.metrics import (
    CrossDeptLinkingPair,
    CrossDeptLinkingPairsResponse,
    ReusabilityDepthResponse,
)
from app.services.metrics_service import MetricsService

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/cross-dept-linking-pairs", response_model=CrossDeptLinkingPairsResponse)
async def cross_dept(
    svc: MetricsService = Depends(get_metrics_service),
) -> CrossDeptLinkingPairsResponse:
    pairs = await svc.cross_dept_linking_pairs()
    return CrossDeptLinkingPairsResponse(
        count=len(pairs),
        pairs=[CrossDeptLinkingPair(**p) for p in pairs],
    )


@router.get("/reusability-depth", response_model=ReusabilityDepthResponse)
async def reusability(
    svc: MetricsService = Depends(get_metrics_service),
) -> ReusabilityDepthResponse:
    items = await svc.reusability_depth()
    from app.schemas.metrics import ReusabilityRow
    return ReusabilityDepthResponse(
        items=[ReusabilityRow(**i) for i in items],
    )
