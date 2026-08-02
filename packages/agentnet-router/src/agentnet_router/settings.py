"""Router 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class LLMSettings:
    """OpenAI 兼容端点(精排 + 本地兜底共用)。"""

    base_url: str
    api_key: str
    model: str


@dataclass
class RouterSettings:
    registry_url: str
    consumer_key: str
    llm: LLMSettings
    threshold: float = 0.65  # 精排置信度低于该值则 fallback 本地
    top_k: int = 10  # 召回候选数
    request_timeout: float = 60.0
    block_secrets: bool = True  # query 含密钥特征时永不外发
    redact_pii: bool = False  # 外发前对 query 做 PII 脱敏
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, prefix: str = "AGENTNET_") -> RouterSettings:
        def env(key: str, default: str = "") -> str:
            return os.environ.get(prefix + key, default)

        return cls(
            registry_url=env("REGISTRY_URL", "http://localhost:9000"),
            consumer_key=env("CONSUMER_KEY"),
            llm=LLMSettings(
                base_url=env("LLM_BASE_URL", "https://api.openai.com/v1"),
                api_key=env("LLM_API_KEY"),
                model=env("LLM_MODEL", "gpt-4o-mini"),
            ),
            threshold=float(env("ROUTE_THRESHOLD", "0.65")),
            top_k=int(env("RECALL_TOP_K", "10")),
            block_secrets=env("BLOCK_SECRETS", "true").lower() not in ("0", "false", "no"),
            redact_pii=env("REDACT_PII", "false").lower() in ("1", "true", "yes"),
        )
