"""Registry 测试公共装置。

默认用临时 SQLite 文件跑(无需 Docker);设置 AGENTNET_TEST_DATABASE_URL
为 postgres URL 时可走 pgvector 路径(需要 docker-compose up)。
agent 侧用 httpx.MockTransport 模拟,embedding 用确定性 HashEmbedding。
"""

import os
import socket
import tempfile

import httpx
import pytest
from agentnet_registry.app import create_app
from agentnet_registry.config import Settings
from agentnet_registry.db import Base, get_engine
from agentnet_registry.security import ensure_admin_key
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ADMIN_KEY = "test-admin-key"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}

_TEST_DB_FILE = os.path.join(tempfile.gettempdir(), "agentnet_test.db")
TEST_DB = os.environ.get("AGENTNET_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_FILE}")


def _db_reachable() -> bool:
    if not TEST_DB.startswith("postgresql"):
        return True
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            return True
    except OSError:
        return False


requires_db = pytest.mark.skipif(not _db_reachable(), reason="postgres 未启动(docker-compose up)")


async def _ensure_pg_db() -> None:
    """postgres 模式下自动建测试库。"""
    if not TEST_DB.startswith("postgresql"):
        return
    base, _, dbname = TEST_DB.rpartition("/")
    engine = create_async_engine(base + "/agentnet", isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": dbname})
            if not exists:
                await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await engine.dispose()


@pytest.fixture
async def app_and_client():
    """每个用例:全新建表 + LifespanManager 启动的应用 + ASGI 客户端。"""
    await _ensure_pg_db()
    settings = Settings(
        database_url=TEST_DB,
        admin_key=ADMIN_KEY,
        embedding_backend="hash",
        inspect_enabled=False,
        canary_enabled=False,
    )
    app = create_app(settings)
    async with LifespanManager(app):
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await ensure_admin_key(ADMIN_KEY)  # drop_all 清掉了 lifespan 写入的 admin
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://registry.test") as client:
            yield app, client


async def make_key(client, role: str, name: str) -> dict:
    resp = await client.post("/v1/keys", json={"role": role, "name": name}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['key']}"}


def make_card(agent_id: str, name: str, natcap: str, netloc: str, caps=None) -> dict:
    return {
        "agent_id": agent_id,
        "name": name,
        "description": f"{name} 简介",
        "natural_capabilities": natcap,
        "capabilities": caps or [],
        "endpoint": f"http://{netloc}",
        "auth": {"type": "bearer", "token": "agent-secret-token"},
    }


def install_world(app, agents: dict) -> None:
    """把 registry 的出站 http 换成 mock 世界。

    agents: {netloc: {"card": dict, "task": dict, "sse": str, "down": bool}}
    """

    def handler(request: httpx.Request) -> httpx.Response:
        spec = agents.get(request.url.host)
        if spec is None or spec.get("down"):
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/card":
            return httpx.Response(200, json=spec["card"])
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/tasks" and request.method == "POST":
            return httpx.Response(200, json=spec["task"])
        task_id = spec["task"]["id"]
        if path == f"/tasks/{task_id}":
            return httpx.Response(200, json=spec["task"])
        if path == f"/tasks/{task_id}/events":
            return httpx.Response(
                200, text=spec["sse"], headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404, json={"error": f"no route {path}"})

    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))


GO_CARD = make_card(
    "go-reviewer",
    "Go Reviewer",
    "我擅长 Go 代码审查:goroutine 泄漏、channel 死锁、pprof 性能分析、并发 bug 排查",
    "go-reviewer.test",
    caps=["go", "code-review"],
)
TRANSLATE_CARD = make_card(
    "translator",
    "Translator",
    "中英互译,文档翻译,支持 markdown 格式保留,术语一致",
    "translator.test",
    caps=["translate"],
)
SQL_CARD = make_card(
    "sql-optimizer",
    "SQL Optimizer",
    "SQL 查询优化,索引建议,执行计划分析,慢查询排查",
    "sql-optimizer.test",
    caps=["sql"],
)


def spec_for(card: dict, task_id: str = "task-1", terminal: str = "completed") -> dict:
    return {
        "card": card,
        "task": {"id": task_id, "agent_id": card["agent_id"], "state": "submitted", "messages": [], "artifacts": []},
        "sse": (
            'event: state\ndata: {"state": "working"}\n\n'
            'event: delta\ndata: {"text": "处理中"}\n\n'
            f'event: state\ndata: {{"state": "{terminal}"}}\n\n'
        ),
    }
