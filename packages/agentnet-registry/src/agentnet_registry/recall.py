"""召回:PostgreSQL 走 pgvector 余弦检索;其它方言(开发/测试 SQLite)用 Python 计算。"""

from __future__ import annotations

import math

from agentnet_core.enums import AgentStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import AgentRow


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return 1.0 - dot / (na * nb)


async def recall_top_k(
    session: AsyncSession, vec: list[float], k: int
) -> list[tuple[AgentRow, float]]:
    """返回 [(row, distance)],按距离升序(越小越相似)。"""
    where = [AgentRow.status == AgentStatus.ACTIVE.value, AgentRow.embedding.is_not(None)]

    if session.bind.dialect.name == "postgresql":
        distance = AgentRow.embedding.cosine_distance(vec).label("distance")
        rows = (
            await session.execute(
                select(AgentRow, distance).where(*where).order_by(distance).limit(k)
            )
        ).all()
        return [(row, float(dist)) for row, dist in rows]

    # fallback:全量取回 Python 计算(仅开发/测试规模)
    rows = (await session.execute(select(AgentRow).where(*where))).scalars().all()
    scored = [(row, _cosine_distance(vec, row.embedding)) for row in rows]
    scored.sort(key=lambda t: t[1])
    return scored[:k]
