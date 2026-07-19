"""兼容 OpenAI ChatCompletion 的异步客户端 (SPEC §3.1/§3.2)。"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    """对 OpenAI 异步 SDK 的一层薄包装，方便 mock。

    API 兼容性:
      - DeepSeek / Qwen / 通义千问 / 智谱 GLM 等通过 OpenAI 协议访问
      - tools 列表用 JSON-Schema 描述
    """

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        s = get_settings()
        self._client = AsyncOpenAI(
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
        response_format: dict[str, str] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """调一次大模型。

        返回结构:
          {
            "content": str | None,                  # 普通文本输出 (一层路由)
            "tool_calls": [
               {"name": ..., "arguments": dict, ...}
            ] | None,                              # Tool Use (二层路由)
            "raw": dict                             # 完整 response 原样
          }
        """
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format
        if tools is not None:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice
                # 强制至少选一个 tool (把 unknown 风险压到大模型自己头上)
            elif tool_choice is None and tools:
                kwargs["tool_choice"] = "required"  # SPEC §3.2 要求"强制开启结构化 Tool 调用"

        logger.debug("LLM chat request: model=%s tools=%s", self._model, bool(tools))
        try:
            resp = await self._client.chat.completions.create(**kwargs)
        except Exception:
            logger.exception("LLM chat 调用失败")
            raise

        return _normalize(resp)


def _normalize(resp: Any) -> dict[str, Any]:
    """把 openai 的 ChatCompletion 转成稳定 dict。"""
    try:
        msg = resp.choices[0].message
        content = getattr(msg, "content", None)
        tool_calls_raw = getattr(msg, "tool_calls", None) or []
        tool_calls: list[dict[str, Any]] = []
        for tc in tool_calls_raw:
            try:
                args = tc.function.arguments
                if isinstance(args, str):
                    parsed = json.loads(args) if args else {}
                else:
                    parsed = args or {}
            except Exception:
                parsed = {}
            tool_calls.append(
                {
                    "id": getattr(tc, "id", None),
                    "name": getattr(tc.function, "name", None),
                    "arguments": parsed,
                }
            )
        return {
            "content": content,
            "tool_calls": tool_calls or None,
            "raw": _to_jsonable(resp),
        }
    except Exception:
        logger.exception("解析 LLM 响应失败")
        return {"content": None, "tool_calls": None, "raw": {}}


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


_SINGLETON: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = LLMClient()
    return _SINGLETON
