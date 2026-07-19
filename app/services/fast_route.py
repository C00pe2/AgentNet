"""一层路由：轻量级意图裁决 (SPEC §3.1)。

max_tokens=10, temperature=0。System Prompt 强制给定的问句。
若 LLM 输出不匹配 agent_id / COMPLEX / UNKNOWN -> 降级为 COMPLEX。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Sequence

from app.db.models import Agent
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)


_FAST_ROUTE_PROMPT_TPL = """你是一个高并发的请求分发器。请阅读以下可用的专家Agent列表及核心能力：
{SHORT_AGENT_LIST_JSON}

请分析用户的输入，并在下面的选项中做出单选，你【必须且只能】从选项中挑选一个作为输出，不要包含任何解释或标点符号：
1. 如果用户意图非常单一，且现有某个专家Agent能够【完美独立搞定】，请直接输出该Agent的 "agent_id"。
2. 如果用户意图复杂、需要多步拆解、或者需要多个专家Agent协同，请直接输出 "COMPLEX"。
3. 如果所有专家Agent都不匹配，请直接输出 "UNKNOWN"。
"""

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")


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
    if _AGENT_ID_RE.match(raw) and raw in valid_ids:
        return raw
    return "COMPLEX"
