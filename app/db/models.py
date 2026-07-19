"""SQLAlchemy 2.x ORM 模型，对应 SPEC §4。

表与字段严格对齐规格书，使用 PostgreSQL 特性：
  - BIGSERIAL (BigInteger + autoincrement)
  - JSONB (from sqlalchemy.dialects.postgresql)
  - 8 个索引全部建上

为方便本地开发/单测，当连接的是非 PostgreSQL 方言时，JSONB 自动降级为 JSON
（SQLAlchemy 的 JSON 类型），保持同等语义。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


class _AdaptiveJSON(TypeDecorator):
    """PostgreSQL 上用真正的 JSONB；其它方言降级为 JSON。"""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class Base(DeclarativeBase):
    pass


class PlanStatus(PyEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Agent(Base):
    __tablename__ = "agents"

    agent_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_dept: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_url: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_token: Mapped[str] = mapped_column(String(255), nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=15000, nullable=False)
    max_retry: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    last_request_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    consecutive_health_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (Index("idx_agents_status", "status"),)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    caller_dept: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    __table_args__ = (Index("idx_sessions_dept", "caller_dept"),)


class PlanExecution(Base):
    __tablename__ = "plan_executions"

    plan_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    session_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("agent_sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    raw_query: Mapped[str] = mapped_column(Text, nullable=False)
    execution_graph: Mapped[dict] = mapped_column(_AdaptiveJSON(), nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), default=PlanStatus.RUNNING.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )

    logs: Mapped[list["AgentCallLog"]] = relationship(
        "AgentCallLog",
        back_populates="plan",
        cascade="save-update",
        passive_deletes=True,
    )

    __table_args__ = (Index("idx_plans_session", "session_id"),)


class AgentCallLog(Base):
    __tablename__ = "agent_call_logs"

    log_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("plan_executions.plan_id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("agent_sessions.session_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_agent_id: Mapped[str] = mapped_column(
        String(100),
        ForeignKey("agents.agent_id"),
        nullable=False,
    )
    step_id: Mapped[int] = mapped_column(Integer, nullable=False)
    request_snapshot: Mapped[dict] = mapped_column(_AdaptiveJSON(), nullable=False, default=dict)
    response_snapshot: Mapped[dict | None] = mapped_column(_AdaptiveJSON(), nullable=True)
    code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.current_timestamp(),
        nullable=False,
    )

    plan: Mapped["PlanExecution | None"] = relationship(
        "PlanExecution", back_populates="logs", passive_deletes=True
    )

    __table_args__ = (
        Index("idx_logs_route", "target_agent_id"),
        Index("idx_logs_session", "session_id"),
        Index("idx_logs_plan", "plan_id"),
        Index("idx_logs_created", "created_at"),
        Index("idx_logs_agent_time", "target_agent_id", "created_at"),
    )


__all__ = ["Base", "Agent", "AgentSession", "PlanExecution", "AgentCallLog", "PlanStatus"]
