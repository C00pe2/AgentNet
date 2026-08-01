"""二层路由：基于 Tool Use 的高级编排器 (SPEC §3.2)。

主动把 active Agent 转成 JSON-Schema tools，强制结构化输出。
约定每个 tool 的 arguments 形如:
{
  "agent_id": "...",
  "sub_query": "... (支持 {{steps.N.output}} 占位符)",
  "depends_on": [0, 1]
}

Anthropic 通道下，``tool_choice`` 在某些 provider (例如本仓库默认的 coding plan) 不
可靠；强约束靠 prompt 兜底。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from app.db.models import Agent
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)


def _build_system_prompt(agents: Sequence[Agent]) -> str:
    """把 active agent 列表嵌进 prompt，强制 LLM 严格从这些 agent_id 里挑选。"""
    lines = [
        "你是企业级 Agent 编排专家。请把用户的请求拆解为一系列可执行步骤。",
        "",
        "【硬约束】",
        "1. 你【必须且只能】调用一个或多个 tool，绝不允许只输出纯文本。",
        "2. 调用 tool 时，arguments.agent_id 必须【严格】从下面列出的可选 agent_id 里挑选，"
        "不允许自创或猜测。如果用户意图不在任何一个 agent 能力范围内，调 tool 'no_match'。",
        "3. 步骤必须形成 DAG，不能出现循环依赖。",
        "4. 子查询要足够具体，让被调用的 Agent 能直接落地。",
        "",
        "【可选 agent_id 列表】",
    ]
    for a in agents:
        lines.append(f"- agent_id={a.agent_id} : {a.name} - {a.description}")
    lines.extend([
        "",
        "【tool 调用约定】每个 tool 的 arguments 是 JSON，包含:",
        "  - agent_id: 必填，必须等于上面列出的某一个 agent_id",
        "  - sub_query: 给该 Agent 的子查询；依赖前序步骤时使用 {{steps.N.output}} 占位符",
        "  - depends_on: 可选，前置 step_id 列表 (从 0 开始)。无依赖时省略",
    ])
    return "\n".join(lines)


_NO_MATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "no_match",
        "description": "当可用 agent_id 列表里没有一个能完成用户意图时调用，reason 说明原因。",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "无法匹配的原因 (中文)"},
            },
            "required": ["reason"],
        },
    },
}


def _agent_to_tool(agent: Agent) -> dict[str, Any]:
    """构造 tool schema。

    Anthropic tool name 受 ``^[a-zA-Z0-9_-]{1,64}$`` 约束，所以这里只把 agent_id 通过
    description 告诉 LLM，真正的 agent_id 仍从 ``tool_use.input.agent_id`` 拿到。
    同步把这些合法字符的不确定性推给 LLMClient 内部 sanitize。
    """
    return {
        "type": "function",
        "function": {
            "name": f"call_{agent.agent_id}",
            "description": (
                f"调用专家 Agent: {agent.name} "
                f"(agent_id 必须严格传 \"{agent.agent_id}\")。"
                f"能力: {agent.description}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": (
                            f"必须是 \"{agent.agent_id}\" (来自上面的可选列表) "
                            "——不允许自创或猜测"
                        ),
                        "enum": [agent.agent_id],
                    },
                    "sub_query": {
                        "type": "string",
                        "description": "给该 Agent 的子查询，可包含 {{steps.N.output}} 占位符",
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "description": "依赖的前置步骤序号 (从 0 开始)。可省略",
                    },
                },
                "required": ["agent_id", "sub_query"],
            },
        },
    }


def build_tools(active_agents: Sequence[Agent]) -> list[dict[str, Any]]:
    tools = [_NO_MATCH_TOOL]
    for a in active_agents:
        tools.append(_agent_to_tool(a))
    return tools


def _decode_arguments(arguments: Any) -> dict[str, Any]:
    """tool_call.arguments 可能是 dict 或 JSON 字符串。"""
    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            return json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def deep_plan(
    *,
    user_query: str,
    active_agents: Sequence[Agent],
    llm: LLMClient,
) -> list[dict[str, Any]]:
    """二层路由，返回解析后的步骤列表 (DAG 拓扑描述)。"""
    if not active_agents:
        return [
            {
                "step_id": 0,
                "agent_id": None,
                "sub_query": "",
                "depends_on": [],
                "tool_call_id": None,
                "raw_arguments": {},
                "no_match": True,
                "reason": "可用 Agent 列表为空，无法编排 (NO_AGENT_MATCHED)",
            }
        ]

    tools = build_tools(active_agents)
    system_prompt = _build_system_prompt(active_agents)
    result = await llm.chat(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_query},
        ],
        tools=tools,
        tool_choice="required",
        max_tokens=2048,
        temperature=0,
    )

    tool_calls = result.get("tool_calls") or []
    if not tool_calls:
        # 兜底: 模型没遵守 hard prompt，直接把 raw content 作为拒绝信号
        raise ValueError(
            f"deep_plan 未获得 tool_calls (大模型未遵守工具约束)。raw={result}"
        )

    valid_ids = {a.agent_id for a in active_agents}
    steps: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        name = tc.get("name") or ""
        args = _decode_arguments(tc.get("arguments"))
        no_match = name == "no_match" or name == "call_no_match"

        # 兜底: 模型选了不在 active 集合的 agent_id → 视为 no_match
        claimed_agent = args.get("agent_id") if not no_match else None
        if claimed_agent and claimed_agent not in valid_ids:
            logger.warning(
                "deep_plan step %d LLM 自创 agent_id=%s 不在 active 集合, 转 no_match",
                i,
                claimed_agent,
            )
            steps.append({
                "step_id": i,
                "agent_id": None,
                "sub_query": "",
                "depends_on": [],
                "tool_call_id": tc.get("id"),
                "raw_arguments": args,
                "no_match": True,
                "reason": f"LLM 给出的 agent_id={claimed_agent} 不在 active 集合",
            })
            continue

        steps.append({
            "step_id": i,
            "agent_id": claimed_agent,
            "sub_query": (args.get("sub_query") or "") if not no_match else "",
            "depends_on": list(args.get("depends_on") or []),
            "tool_call_id": tc.get("id"),
            "raw_arguments": args,
            "no_match": no_match,
            "reason": args.get("reason") if no_match else None,
        })

    if all(s["no_match"] for s in steps):
        return steps[:1]  # 收敛到一个 no_match 标记

    return _prune_to_real_steps(steps)


def _prune_to_real_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并 no_match 标记，只返回真实可执行步骤。"""
    real = [s for s in steps if not s["no_match"]]
    if not real:
        first = steps[0]
        return [
            {
                "step_id": 0,
                "agent_id": None,
                "sub_query": "",
                "depends_on": [],
                "tool_call_id": first.get("tool_call_id"),
                "raw_arguments": first.get("raw_arguments", {}),
                "no_match": True,
                "reason": first.get("reason") or "Planner 判定无可用 Agent",
            }
        ]
    return real

