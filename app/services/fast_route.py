"""一层路由：轻量级意图裁决 (SPEC §3.1)。

max_tokens=10, temperature=0。System Prompt 强制给定的问句。
若 LLM 输出不匹配 agent_id / COMPLEX / UNKNOWN -> 降级为 COMPLEX。
"""
from __future__ import annotations

import json
import logging
from typing import Sequence

from app.db.models import Agent
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)


_FAST_ROUTE_PROMPT_TPL = """你是一个高并发的请求分发器 (1-shot 分类)。阅读以下可用的专家 Agent 列表:
{SHORT_AGENT_LIST_JSON}

【硬约束】只允许输出一个 token / 短语, 不要任何解释、标点、换行:
- 如果单个 Agent 能完美独立搞定, 输出该 agent 的 agent_id
- 如果需要多步拆解 / 多 Agent 协同, 输出: COMPLEX
- 如果所有 Agent 都不匹配, 输出: UNKNOWN

不要写句子, 不要解释, 不要带引号。直接给最终答案。"""


def _build_short_list(agents: Sequence[Agent]) -> str:
    items = [
        {
            "agent_id": a.agent_id,
            "name": a.name,
            "description": a.description,
        }
        for a in agents
    ]
    return json.dumps(items, ensure_ascii=False)


async def fast_route(
    *,
    user_query: str,
    active_agents: Sequence[Agent],
    llm: LLMClient,
) -> tuple[str, str]:
    """一层路由裁决。

    Returns:
        (decision, reason)
        decision ∈ {"COMPLEX", "UNKNOWN", "<agent_id>"}
    """
    short_list = _build_short_list(active_agents)
    if not active_agents:
        return ("UNKNOWN", "no active agents")

    system_prompt = _FAST_ROUTE_PROMPT_TPL.format(SHORT_AGENT_LIST_JSON=short_list)
    try:
        result = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query},
            ],
            max_tokens=10,
            temperature=0.0,
        )
    except Exception:
        logger.exception("fast_route LLM 调用失败，降级为 COMPLEX")
        return ("COMPLEX", "fast_route_llm_error")

    raw = (result.get("content") or "").strip()
    decision = _normalize(raw, valid_ids={a.agent_id for a in active_agents})
    return (decision, raw)


def _normalize(raw: str, *, valid_ids: set[str]) -> str:
    raw = raw.strip().strip('"\'`').strip()
    if not raw:
        return "COMPLEX"
    upper = raw.upper()
    if upper == "COMPLEX":
        return "COMPLEX"
    if upper == "UNKNOWN":
        return "UNKNOWN"
    if raw in valid_ids:
        return raw
    return "COMPLEX"
