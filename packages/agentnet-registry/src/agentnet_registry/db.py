"""数据库:引擎、会话、ORM 模型。

六张表:
  api_keys             - API key(存 sha256,不存明文)
  agents               - Agent Card 存储 + embedding 向量
  call_logs            - Gateway 调用记录(信誉数据源)
  canary_logs          - canary 用例跑分记录(信誉数据源)
  credit_accounts      - 消费者积分余额
  credit_transactions  - 积分流水(充值/扣费)
"""

from __future__ import annotations

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

EMBEDDING_DIM = 1024  # bge-m3 dense 维度;更换模型需同步迁移


class VectorType(TypeDecorator):
    """PostgreSQL 用 pgvector;其它方言(开发/测试用 SQLite)退化为 JSON 数组,
    此时召回在 recall.py 里用 Python 计算余弦(仅适合小规模)。"""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(EMBEDDING_DIM))
        return dialect.type_descriptor(JSON())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16))  # admin / provider / consumer
    name: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AgentRow(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    natural_capabilities: Mapped[str] = mapped_column(Text, default="")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    canary_cases: Mapped[list] = mapped_column(JSON, default=list)
    endpoint: Mapped[str] = mapped_column(String(512))
    auth_type: Mapped[str] = mapped_column(String(16), default="bearer")
    auth_token: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pricing: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[str] = mapped_column(String(32), default="0.1.0")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    consecutive_health_failures: Mapped[int] = mapped_column(Integer, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CallLogRow(Base):
    __tablename__ = "call_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("agents.agent_id"), index=True)
    consumer: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    final_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    charge: Mapped[float | None] = mapped_column(Float, nullable=True)  # 本次调用定价快照(积分)


class CanaryLogRow(Base):
    __tablename__ = "canary_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(64), ForeignKey("agents.agent_id"), index=True)
    query: Mapped[str] = mapped_column(Text)
    passed: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[str] = mapped_column(Text, default="")  # 未通过原因(缺失关键词/错误)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CreditAccountRow(Base):
    __tablename__ = "credit_accounts"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)  # consumer 身份(key 属主名)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CreditTxRow(Base):
    __tablename__ = "credit_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    delta: Mapped[float] = mapped_column(Float)  # 正=充值,负=扣费
    reason: Mapped[str] = mapped_column(String(16))  # topup | charge
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# ---------------------------------------------------------------------------
# 引擎 / 会话(进程级单例)
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


def init_engine(database_url: str) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = create_async_engine(database_url, pool_size=10, max_overflow=20)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    assert _engine is not None, "engine 未初始化,请先调用 init_engine()"
    return _engine


def get_sessionmaker() -> async_sessionmaker:
    assert _sessionmaker is not None, "engine 未初始化,请先调用 init_engine()"
    return _sessionmaker


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
