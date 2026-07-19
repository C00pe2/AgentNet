"""request_id 上下文工具。

SPEC §2.2 要求所有响应必须携带 request_id (e.g. req-9a8b7c6d-5e4f)。
使用 contextvars 把它在整个异步调用栈中传播。
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

_REQUEST_ID_VAR: ContextVar[str | None] = ContextVar("agentnet_request_id", default=None)


class RequestIdContext:
    @staticmethod
    def set(request_id: str) -> None:
        _REQUEST_ID_VAR.set(request_id)

    @staticmethod
    def get() -> str | None:
        return _REQUEST_ID_VAR.get()

    @staticmethod
    def clear() -> None:
        _REQUEST_ID_VAR.set(None)


def new_request_id() -> str:
    """形如 req-9a8b7c6d5e4f 全小写 hex 短串。"""
    return "req-" + uuid.uuid4().hex[:12]
