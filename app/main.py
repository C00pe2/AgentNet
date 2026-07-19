"""AgentNet Gateway - FastAPI 入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import agents as agents_api
from app.api import chat as chat_api
from app.api import health_admin as health_api
from app.api import metrics as metrics_api
from app.api import sessions as sessions_api
from app.config import get_settings
from app.core.codes import ErrorCode, GatewayError
from app.core.request_id import RequestIdContext, new_request_id
from app.db.session import dispose_engine, init_engine
from app.scheduler import build_scheduler, shutdown_scheduler
from app.services.expert_client import close_expert_client, get_expert_client
from app.services.health_service import HealthService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
logger = logging.getLogger("agentnet")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 测试时可以跳过 lifespan (AGENTNET_SKIP_LIFESPAN=1)
    import os
    if os.environ.get("AGENTNET_SKIP_LIFESPAN") == "1":
        logger.info("lifespan skipped (AGENTNET_SKIP_LIFESPAN=1)")
        try:
            yield
        finally:
            logger.info("test shutdown complete")
        return

    init_engine(settings.database_url)
    expert = get_expert_client()
    await expert.start()
    health = HealthService()
    await health.start()
    scheduler = build_scheduler(health=health)
    scheduler.start()
    app.state.health = health
    app.state.expert = expert
    app.state.scheduler = scheduler
    logger.info("AgentNet Gateway 启动完毕")

    try:
        yield
    finally:
        logger.info("AgentNet Gateway 关闭中...")
        await shutdown_scheduler(scheduler)
        await health.close()
        await close_expert_client()
        await dispose_engine()
        logger.info("AgentNet Gateway 关闭完成")


def _orjson_response(payload: dict, status_code: int = 200) -> Response:
    """用 ensure_ascii=False 的 JSONResponse，兼容中文。"""
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"content-type": "application/json; charset=utf-8"},
    )


class UTF8JSONResponse(JSONResponse):
    """ensure_ascii=False 的 JSONResponse，确保中文 / 多字节字符不被 \\uXXXX 转义。"""

    def render(self, content) -> bytes:
        import json as _json

        return _json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


app = FastAPI(
    title="AgentNet Gateway",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)

# request_id 中间件
@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or new_request_id()
    RequestIdContext.set(rid)
    try:
        resp: Response = await call_next(request)
    finally:
        RequestIdContext.clear()
    resp.headers["X-Request-ID"] = rid
    return resp


# 全局异常处理 - 所有错误一律 Envelope
@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError) -> Response:
    from app.schemas.response import error_envelope

    body = error_envelope(exc.code, exc.message, data=exc.data).model_dump()
    return _orjson_response(body, status_code=exc.http_status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    from app.schemas.response import error_envelope

    body = error_envelope(
        ErrorCode.BAD_REQUEST,
        f"请求参数错误: {exc.errors()[0]['msg'] if exc.errors() else 'invalid'}",
    ).model_dump()
    return _orjson_response(body, status_code=400)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    from app.schemas.response import error_envelope

    code = ErrorCode.BAD_REQUEST
    if exc.status_code == 404:
        # 这里区分: 来自路由 raise 的 404 我们保持 400 (BAD_REQUEST)
        pass
    if exc.status_code == 405:
        code = ErrorCode.BAD_REQUEST
    if exc.status_code in (401, 403):
        code = ErrorCode.BAD_REQUEST
    body = error_envelope(code, str(exc.detail) if exc.detail else "Bad Request").model_dump()
    return _orjson_response(body, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    from app.schemas.response import error_envelope

    logger.exception("未捕获异常: %s", exc)
    body = error_envelope(
        ErrorCode.PLAN_ORCHESTRATION_FAILED,
        f"网关内部异常: {type(exc).__name__}: {str(exc)[:200]}",
    ).model_dump()
    return _orjson_response(body, status_code=500)


# 路由注册
app.include_router(agents_api.router)
app.include_router(sessions_api.router)
app.include_router(chat_api.router)
app.include_router(metrics_api.router)
app.include_router(health_api.router)


@app.get("/")
async def root() -> dict:
    return {
        "name": "AgentNet Gateway",
        "version": "1.0.0",
        "endpoints": [
            "POST /agents",
            "POST /sessions",
            "POST /chat",
            "GET  /metrics/cross-dept-linking-pairs",
            "GET  /metrics/reusability-depth",
            "GET  /admin/health/circuit-breaker",
        ],
    }


@app.get("/healthz")
async def healthz() -> dict:
    """轻量 liveness，不依赖下游。"""
    return {"ok": True}
