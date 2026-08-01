"""LLM 适配层：Anthropic Messages API 异步客户端 (兼容 MiniMax coding plan)。

API 协议对齐 `https://api.minimaxi.com/anthropic` 通道使用的 Anthropic Messages API。
为不破坏上层调用约定 (fast_route / deep_plan)，这里把 OpenAI 风格的输入自动转成
Anthropic 风格；响应再做一次反向归一，让 `LLMClient.chat` 对外暴露的 dict 结构稳定。
"""
from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    """Anthropic 协议下的网关 LLM 客户端。

    对外接口保持稳定 (OpenAI-ChatCompletion 形态的 dict 返回值)，
    内部负责:
      - OpenAI 的 `messages` 列表 -> Anthropic 的 `system` + 过滤后的 `messages`
      - OpenAI 的 `tools` (function 形态) -> Anthropic 的 `tools` (name/description/input_schema)
      - OpenAI 的 `tool_choice` ("required"/"auto"/dict) -> Anthropic 的 tool_choice enum
      - Anthropic 响应 content blocks (text/tool_use) -> flat dict {content, tool_calls}
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        s = get_settings()
        self._client = AsyncAnthropic(
            base_url=base_url or s.llm_base_url,
            api_key=api_key or s.llm_api_key,
        )
        self._model = model or s.llm_model

    @property
    def model(self) -> str:
        return self._model

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        response_format: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """调一次大模型。

        返回结构 (兼容上层):
          {
            "content": str | None,        # 一层路由 (fast_route) 文本输出
            "tool_calls": [
              {"id": ..., "name": ..., "arguments": dict}
            ] | None,                    # 二层路由 (deep_plan) Tool Use
            "raw": <原始 Anthropic response>
          }
        """
        system_prompt, anthropic_messages = self._split_system(messages)
        anthropic_tools = self._convert_tools(tools)
        anthropic_tool_choice = self._convert_tool_choice(tool_choice, tools)

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = 1024  # Anthropic 必填，给一个合理兜底
        if temperature is not None:
            kwargs["temperature"] = temperature
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
            if anthropic_tool_choice is not None:
                kwargs["tool_choice"] = anthropic_tool_choice
        # response_format 不直接受 Anthropic 支持 (它没有 json_object mode);
        # 由 prompt 强制或上层解析兜底。这里忽略。
        if response_format is not None:
            logger.debug("Anthropic 后端忽略 response_format=%s", response_format)

        logger.debug(
            "Anthropic chat model=%s tools=%d max_tokens=%s",
            self._model,
            len(anthropic_tools or []),
            max_tokens,
        )
        try:
            resp = await self._client.messages.create(**kwargs)
        except Exception:
            logger.exception("Anthropic chat 调用失败")
            raise

        return _normalize(resp)

    # -------------------------------------------------
    # 输入转换
    # -------------------------------------------------
    @staticmethod
    def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Anthropic 要求 `system` 是顶层参数。

        我们把第一条 role=='system' 的消息抽出来作为顶层 system，其余按
        user/assistant 顺序整理（Anthropic 要求首条消息必须是 user，因此首条
        如果是 system 会被剥掉，剩下的第一条必须是 user）。
        """
        system_prompt = ""
        rest: list[dict[str, Any]] = []
        found_system = False
        for m in messages:
            role = m.get("role")
            content = m.get("content", "") or ""
            if role == "system" and not found_system:
                system_prompt = content if isinstance(content, str) else json_dumps(content)
                found_system = True
                continue
            # Anthropic 不允许 system role 出现于 messages 列表里
            if role == "system":
                # 后续若还有 system，append 到主 system_prompt 里
                if isinstance(content, str):
                    system_prompt += "\n\n" + content
                continue
            rest.append({"role": role, "content": content})

        # Anthropic 要求首条消息 role == user；若是 assistant 则前置一个空 user
        if rest and rest[0]["role"] != "user":
            rest.insert(0, {"role": "user", "content": "(continue)"})

        return system_prompt, rest

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """OpenAI tool 形态 -> Anthropic tool 形态。

        OpenAI:
          {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
        Anthropic:
          {"name": ..., "description": ..., "input_schema": {...}}

        Anthropic 要求 tool name 满足 ``^[a-zA-Z0-9_-]{1,64}$`` —— 任何 `.` / 空格 / 中文
        都会被替换为 `_`，并截断到 64 字符。真正的 agent_id 永远从
        ``tool_use.input.agent_id`` 拿到，所以 name 仅作调用标记。
        """
        if not tools:
            return None
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for t in tools:
            if t.get("type") == "function" and isinstance(t.get("function"), dict):
                fn = t["function"]
                schema = fn.get("parameters") or {"type": "object", "properties": {}}
                raw_name = str(fn.get("name", "tool"))
            elif "name" in t:
                raw_name = str(t["name"])
                schema = t.get("input_schema") or {"type": "object", "properties": {}}
            else:
                continue

            safe = _sanitize_tool_name(raw_name)
            # 唯一化，避免重复
            base = safe
            n = 1
            while safe in seen:
                n += 1
                safe = f"{base[:60]}_{n}"
            seen.add(safe)

            desc = t.get("description") if isinstance(t, dict) else ""
            if not desc and t.get("type") == "function":
                desc = fn.get("description", "")

            out.append({
                "name": safe,
                "description": desc or "",
                "input_schema": schema,
            })
        return out or None

    @staticmethod
    def _convert_tool_choice(
        choice: str | dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """OpenAI tool_choice -> Anthropic tool_choice。

        OpenAI "required" → Anthropic {"type": "any"}（强制模型选一个 tool）
        OpenAI "auto"     → Anthropic {"type": "auto"}
        OpenAI dict       → Anthropic {"type": "tool", "name": ...}
        未指定但有 tools  → Anthropic {"type": "any"}（SPEC §3.2 要求强制）
        """
        if not tools:
            return None
        if choice is None:
            return {"type": "any"}
        if isinstance(choice, str):
            if choice == "required":
                return {"type": "any"}
            if choice == "auto":
                return {"type": "auto"}
            if choice == "none":
                return {"type": "auto"}
            return {"type": "any"}
        if isinstance(choice, dict):
            t = choice.get("type")
            if t == "tool":
                return {"type": "tool", "name": choice.get("name", "")}
            if t in ("auto", "any"):
                return {"type": t}
        return {"type": "any"}


def _normalize(resp: Any) -> dict[str, Any]:
    """把 Anthropic Message 转成稳定 dict。

    Anthropic Message 结构:
      resp.content: list of ContentBlock
        - {"type": "text", "text": "..."}
        - {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
      resp.stop_reason: "end_turn" | "tool_use" | "max_tokens" | ...
    """
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    blocks = getattr(resp, "content", None) or []
    for b in blocks:
        btype = getattr(b, "type", None) or (b.get("type") if isinstance(b, dict) else None)
        if btype == "text":
            text_parts.append(getattr(b, "text", "") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": getattr(b, "id", None),
                "name": getattr(b, "name", None),
                "arguments": getattr(b, "input", None) or {},
            })
        # 其它类型 (thinking/tool_result) 直接忽略
    return {
        "content": "".join(text_parts) or None,
        "tool_calls": tool_calls or None,
        "raw": _to_jsonable(resp),
    }


def _to_jsonable(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    return obj


def json_dumps(o: Any) -> str:
    import json as _json

    return _json.dumps(o, ensure_ascii=False)


import re as _re_name

_TOOL_NAME_INVALID_RE = _re_name.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_tool_name(raw: str) -> str:
    """把任意字符串变成 Anthropic tool name 合法的形态。

    规则:
      - 仅允许 ``a-zA-Z0-9_-``
      - 其它字符 (`.`、`/`、中文、空格等) 一律替换为 `_`
      - 长度上限 64 字符
      - 空字符串兜底为 ``tool``
    """
    if not raw:
        return "tool"
    safe = _TOOL_NAME_INVALID_RE.sub("_", raw)
    safe = safe.strip("_") or "tool"
    if len(safe) > 64:
        safe = safe[:64].rstrip("_") or "tool"
    return safe


_SINGLETON: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = LLMClient()
    return _SINGLETON
