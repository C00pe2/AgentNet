"""二层路由：基于 Tool Use 的高级编排器 (SPEC §3.2)。

主动把 active Agent 转成 JSON-Schema tools，强制 tool_choice=required。
约定每个 tool 的 arguments 形如:
{
  "agent_id": "...",
  "sub_query": "... (支持 {{steps.N.output}} 占位符)",
  "depends_on": [0, 1]
}
"""
from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from app.db.models import Agent
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)


_DEEP_PLAN_SYSTEM_PROMPT = """你是一个企业级 Agent 编排专家。请把用户的请求拆解为一系列可执行步骤。

约束:
1. 每个步骤必须通过调用对应的 tool 来执行，不允许输出纯文本。
2. 每个 tool 的 arguments 是 JSON，包含:
   - agent_id: 必填，从可用 agent 列表里挑一个 agent_id
   - sub_query: 给该 Agent 的子查询。若依赖前序步骤，使用占位符 {{steps.N.output}}，N 是步骤序号
   - depends_on: 可选，依赖的前置 step_id 列表 (整数数组)。无依赖时省略
3. 步骤必须形成 DAG，不能出现循环依赖。
4. 子查询要足够具体，让被调用的专家 Agent 能直接落地。
5. 如果没有任何一个 agent 能完成用户意图，调用 tool "no_match" 并传 {"reason": "..."}。
"""


_NO_MATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "no_match",
        "description": "当可用专家 Agent 列表中没有任何一个能完成用户意图时调用。",
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
    return {
        "type": "function",
        "function": {
            "name": f"call_{agent.agent_id}",
            "description": f"{agent.name} - {agent.description}",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "const": agent.agent_id,
                        "description": "本步骤负责执行的 Agent 标识",
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
    """二层路由，返回解析后的步骤列表 (DAG 拓扑描述)。

    Returns:
        list of step dicts:
          {
            "step_id": int,
            "agent_id": str | None,
            "sub_query": str,
            "depends_on": list[int],
            "tool_call_id": str | None,
            "raw_arguments": dict,
            "no_match": bool,
            "reason": str | None,
          }
    """
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
    result = await llm.chat(
        messages=[
            {"role": "system", "content": _DEEP_PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": user_query},
        ],
        tools=tools,
        tool_choice="required",
    )

    tool_calls = result.get("tool_calls") or []
    if not tool_calls:
        raise ValueError("deep_plan 未获得任何 tool_calls (大模型格式错误)")

    steps: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        name = tc.get("name") or ""
        args = _decode_arguments(tc.get("arguments"))
        no_match = name == "no_match"
        steps.append(
            {
                "step_id": i,
                "agent_id": args.get("agent_id") if not no_match else None,
                "sub_query": (args.get("sub_query") or "") if not no_match else "",
                "depends_on": list(args.get("depends_on") or []),
                "tool_call_id": tc.get("id"),
                "raw_arguments": args,
                "no_match": no_match,
                "reason": args.get("reason") if no_match else None,
            }
        )

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
