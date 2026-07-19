"""异步 SQLAlchemy 引擎与 Session 工厂 (lifespan 友好)。"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str | None = None) -> AsyncEngine:
    """初始化全局引擎。仅在 FastAPI lifespan 启动阶段调用一次。"""
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    url = database_url or get_settings().database_url
    _engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        future=True,
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    logger.info("Async SQLAlchemy engine initialized: %s", _redact(url))
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        return init_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI Depends 用：每个请求一个 Session。"""
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


def close_engine() -> None:
    """同步关闭入口 (仅供脚本退出时使用)。"""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        return
    asyncio.run(dispose_engine())


def _redact(url: str) -> str:
    """把 URL 里的 password 段改成 ***，避免日志泄漏。"""
    if "@" not in url:
        return url
    head, tail = url.rsplit("@", 1)
    if ":" in head:
        scheme_user, _ = head.rsplit(":", 1)
        return f"{scheme_user}:***@{tail}"
    return url
