"""统一响应 envelope (SPEC §2.2)。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.codes import ErrorCode
from app.core.request_id import RequestIdContext

T = TypeVar("T")


class Envelope(BaseModel, Generic[T]):
    """网关对外统一响应结构。"""

    request_id: str
    code: int
    message: str
    data: T | None = None


def success_envelope(data: Any, *, message: str = "Success") -> Envelope[Any]:
    rid = RequestIdContext.get() or ""
    return Envelope(
        request_id=rid,
        code=int(ErrorCode.SUCCESS),
        message=message,
        data=data,
    )


def error_envelope(
    code: ErrorCode | int,
    message: str,
    *,
    data: Any | None = None,
) -> Envelope[Any]:
    rid = RequestIdContext.get() or ""
    c = int(code)
    return Envelope(
        request_id=rid,
        code=c,
        message=message,
        data=data,
    )


__all__ = ["Envelope", "success_envelope", "error_envelope"]
