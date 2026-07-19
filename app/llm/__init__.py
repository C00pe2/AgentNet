"""LLM 适配层：兼容 OpenAI ChatCompletion 接口的异步客户端。

支持:
  - 普通 chat_completion
  - tools 强结构化输出 (Tool Use)
"""
from app.llm.client import LLMClient, get_llm_client

__all__ = ["LLMClient", "get_llm_client"]
