"""会话 API。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.db.models import AgentSession
from app.deps import get_session_service
from app.schemas.response import Envelope, success_envelope
from app.schemas.session import SessionCreate, SessionRead
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=Envelope[SessionRead])
async def create_session(
    payload: SessionCreate,
    svc: SessionService = Depends(get_session_service),
) -> Envelope[SessionRead]:
    record = AgentSession(
        session_id=payload.session_id,
        user_id=payload.user_id,
        caller_dept=payload.caller_dept,
    )
    await svc.upsert(record)
    await svc.session.commit()
    return success_envelope(
        SessionRead(
            session_id=record.session_id,
            user_id=record.user_id,
            caller_dept=record.caller_dept,
            created_at=record.created_at,
        )
    )


@router.get("/{session_id}", response_model=Envelope[SessionRead])
async def get_session(
    session_id: str,
    svc: SessionService = Depends(get_session_service),
) -> Envelope[SessionRead]:
    s = await svc.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return success_envelope(
        SessionRead(
            session_id=s.session_id,
            user_id=s.user_id,
            caller_dept=s.caller_dept,
            created_at=s.created_at,
        )
    )
