"""LLM 调用:OpenAI 兼容 chat completions + 精排 prompt。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import httpx

from .client import Candidate
from .settings import LLMSettings

RERANK_SYSTEM = """你是 AgentNet 网络的路由器。给你用户问题和一组候选 Agent 的能力名片,
判断是否有某个 Agent 明显适合解决这个问题。

只输出一行 JSON,不要输出任何其他内容:
{"agent_id": "<选中的候选 agent_id;没有合适的则为 null>", "confidence": <0.0~1.0 的把握>, "reason": "<一句话理由>"}

规则:
- 只选能力名片与问题明确匹配的 Agent,不要把问题硬塞给不合适的 Agent
- confidence 要诚实:候选与问题越匹配越高;候选都很勉强时应选 null
- 参考信誉数据(成功率高的优先),但能力匹配是第一位的"""


@dataclass
class RerankResult:
    agent_id: str | None
    confidence: float
    reason: str


async def chat(
    settings: LLMSettings,
    messages: list[dict],
    temperature: float = 0.0,
    http: httpx.AsyncClient | None = None,
    timeout: float = 60.0,
) -> str:
    """一次 OpenAI 兼容 chat completion,返回文本内容。"""
    own = http is None
    client = http or httpx.AsyncClient()
    try:
        resp = await client.post(
            f"{settings.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            json={"model": settings.model, "messages": messages, "temperature": temperature},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    finally:
        if own:
            await client.aclose()


def build_rerank_prompt(query: str, candidates: list[Candidate]) -> str:
    lines = [f"用户问题:{query}", "", "候选 Agent:"]
    for i, c in enumerate(candidates, 1):
        card = c.card
        rep = card.reputation
        rep_text = (
            f"调用 {rep.calls} 次,成功率 {rep.success_rate:.0%}"
            + (f",评分 {rep.rating:.1f}" if rep.rating is not None else "")
            + (f",canary 通过率 {rep.canary_score:.0%}" if rep.canary_score is not None else "")
            if rep
            else "新注册,无记录"
        )
        lines.append(
            f"{i}. agent_id={card.agent_id}\n"
            f"   名称:{card.name}\n"
            f"   简介:{card.description}\n"
            f"   能力:{card.natural_capabilities}\n"
            f"   标签:{', '.join(card.capabilities)}\n"
            f"   信誉:{rep_text}\n"
            f"   召回相似度:{c.score:.2f}"
        )
    return "\n".join(lines)


def parse_rerank_response(content: str) -> RerankResult:
    """容错解析 LLM 输出的 JSON(允许 ```json 围栏和前后杂文本)。"""
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    data = json.loads(text)
    agent_id = data.get("agent_id")
    return RerankResult(
        agent_id=str(agent_id) if agent_id else None,
        confidence=float(data.get("confidence", 0.0)),
        reason=str(data.get("reason", "")),
    )


async def rerank(
    query: str,
    candidates: list[Candidate],
    settings: LLMSettings,
    http: httpx.AsyncClient | None = None,
) -> RerankResult:
    content = await chat(
        settings,
        messages=[
            {"role": "system", "content": RERANK_SYSTEM},
            {"role": "user", "content": build_rerank_prompt(query, candidates)},
        ],
        temperature=0.0,
        http=http,
    )
    return parse_rerank_response(content)
