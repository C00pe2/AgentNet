"""数据库初始化 + 默认 seed。

用法:
    python -m scripts.seed             # 创建表 + 默认数据
    python -m scripts.seed --reset     # 重新建表
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.config import get_settings
from app.db.models import Base
from app.db.session import dispose_engine, get_engine, get_sessionmaker
from app.db.models import Agent, AgentSession
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


DEFAULT_AGENTS: list[dict] = [
    {
        "agent_id": "corp.tech.security.code-auditor",
        "name": "Java底层代码安全审计专家",
        "owner_dept": "技术保障部-安全组",
        "description": "专门审查 Java/Spring 框架底层的反序列化、SQL 注入、XXE 等漏洞。",
        "endpoint_url": "http://10.24.56.12:8080/api/v1/chat",
        "auth_token": "st_abc123789xyz",
        "timeout_ms": 15000,
        "max_retry": 1,
    },
    {
        "agent_id": "corp.fin.audit.invoice-reviewer",
        "name": "发票合规审查专家",
        "owner_dept": "财务部-稽核组",
        "description": "审查发票的合规性、税号匹配、金额是否超出业务规范。",
        "endpoint_url": "http://10.24.56.20:8080/api/v1/chat",
        "auth_token": "st_fin_invoice_2024",
        "timeout_ms": 20000,
        "max_retry": 1,
    },
    {
        "agent_id": "corp.hr.policy.qa",
        "name": "HR政策问答专家",
        "owner_dept": "人力资源部-政策组",
        "description": "回答员工关于假期、报销、培训、晋升等常见政策问题。",
        "endpoint_url": "http://10.24.56.30:8080/api/v1/chat",
        "auth_token": "st_hr_policy_2024",
        "timeout_ms": 10000,
        "max_retry": 1,
    },
]

DEFAULT_SESSIONS: list[dict] = [
    {
        "session_id": "demo-session-001",
        "user_id": "u-alice",
        "caller_dept": "研发中心-后端组",
    }
]


async def reset_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    logger.info("已重建 schema (drop + create)")


async def ensure_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, checkfirst=True)
    logger.info("已确认 schema (create_all checkfirst)")


async def seed() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            for row in DEFAULT_AGENTS:
                await session.merge(Agent(**row))
            for row in DEFAULT_SESSIONS:
                await session.merge(AgentSession(**row))
    logger.info("seed 完成: %d 个 agent / %d 个 session", len(DEFAULT_AGENTS), len(DEFAULT_SESSIONS))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop + create 全表")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )

    s = get_settings()
    init_engine = None  # noqa: F841 - silence
    from app.db.session import init_engine as _init_engine
    _init_engine(s.database_url)
    if args.reset:
        await reset_schema()
    else:
        await ensure_schema()
    await seed()
    await dispose_engine()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
