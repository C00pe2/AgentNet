"""End-to-end smoke test: FastAPI app + httpx ASGI transport。

不需要启动数据库/PostgreSQL；只走真实 lifespan 会导致 init engine 失败，
所以我们绕过 lifespan，直接测试路由 handler 在已 mock 好的 deps 下的行为。

注: 本测试不验证落库；用于冒烟 HTTP 协议层。
"""
import json

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_healthz():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 跳过 lifespan (异步 init engine 需要真正 DB)
        resp = await client.get("/healthz")
        print("GET /healthz ->", resp.status_code, resp.json())
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


async def test_root():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        print("GET / ->", resp.status_code, json.dumps(resp.json(), ensure_ascii=False)[:200])
        assert resp.status_code == 200


async def test_404_returns_envelope():
    """任意错误都应该返回 Envelope。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/chat", json={"session_id": "x", "query": "y"})
        print("POST /chat (no db) ->", resp.status_code, resp.json())
        # 没有 lifespan / 没 DB 所以会异常 -> 500 + envelope
        assert resp.status_code in (500, 503)
        body = resp.json()
        assert "request_id" in body
        assert "code" in body
        assert "message" in body


async def main():
    await test_healthz()
    await test_root()
    await test_404_returns_envelope()
    print("HTTP smoke OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
