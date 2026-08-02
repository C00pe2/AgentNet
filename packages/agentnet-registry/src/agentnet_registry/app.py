"""FastAPI 应用装配:lifespan、统一 Envelope 异常处理、request-id。"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import httpx
from agentnet_core import constants
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .canary import CanaryRunner
from .config import Settings
from .db import get_sessionmaker, init_db, init_engine
from .embedding import build_embedder
from .federation import FederationSync
from .governance import CircuitBreaker, RateLimiter
from .inspector import Inspector
from .routes import ok, router
from .security import ensure_admin_key


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers[constants.REQUEST_ID_HEADER] = uuid.uuid4().hex[:16]
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = init_engine(settings.database_url)
        await init_db(engine)
        sm = get_sessionmaker()
        await ensure_admin_key(settings.admin_key)

        app.state.settings = settings
        app.state.session_maker = sm
        app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(None))
        app.state.embedder = build_embedder(settings)
        app.state.rate_limiter = RateLimiter(settings.rate_limit_rps)
        app.state.breaker = CircuitBreaker(
            settings.breaker_fail_threshold, settings.breaker_open_sec, settings.breaker_window_sec
        )

        inspector = Inspector(sm, app.state.http, settings.inspect_interval_sec, settings.inspect_fail_threshold)
        inspect_task = (
            asyncio.create_task(inspector.run()) if settings.inspect_enabled else None
        )
        canary = CanaryRunner(sm, app.state.http, settings.canary_interval_sec, settings.canary_timeout_sec)
        canary_task = (
            asyncio.create_task(canary.run()) if settings.canary_enabled else None
        )
        federation = FederationSync(
            sm, app.state.http, app.state.embedder, settings.peers, settings.federation_sync_interval_sec
        )
        federation_task = (
            asyncio.create_task(federation.run())
            if settings.federation_enabled and settings.peers
            else None
        )

        yield

        inspector.stop()
        canary.stop()
        federation.stop()
        for task in (inspect_task, canary_task, federation_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await app.state.http.aclose()
        await engine.dispose()

    app = FastAPI(title="AgentNet Registry", version=__version__, lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(router, prefix=constants.API_PREFIX)

    @app.exception_handler(HTTPException)
    async def http_exc_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": str(exc.detail), "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', []))}: {e.get('msg')}" for e in exc.errors()[:5])
        return JSONResponse(status_code=422, content={"code": 422, "message": details, "data": None})

    @app.exception_handler(Exception)
    async def unhandled_exc_handler(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": f"内部错误({exc.__class__.__name__})", "data": None},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return ok({"status": "up"})

    return app
