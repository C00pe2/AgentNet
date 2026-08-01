"""AgentNet 配置文件。

加载优先级 (后者覆盖前者):
  1. pydantic-settings 默认值 (env/`.env`)
  2. `config/configs.json` (内部凭据文件，默认被 `.gitignore` 排除)
  3. 环境变量 (AGENTNET_*)

`config/configs.json` 用于安放不便入仓的凭据 (例如 LLM API key)。
它出现后，自动注入 `llm_api_key` / `llm_base_url` / `llm_model` 三个字段，
并且不破坏 pydantic-settings 的 env / `.env` 优先级。
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "configs.json"


def _load_configs_json() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("config/configs.json 读取失败, 走环境变量兜底: %s", exc)
        return {}


def _apply_configs_env(cfg: dict) -> None:
    """把 configs.json 里的 llm 字段直接写进 os.environ，让 pydantic-settings 自然读取。

    这一步在 Settings 初始化之前触发，所以不会污染 `lru_cache`。
    """
    llm = cfg.get("llm") or {}
    pairs: dict[str, str] = {}
    if llm.get("base_url"):
        pairs["AGENTNET_LLM_BASE_URL"] = str(llm["base_url"])
    if llm.get("api_key"):
        pairs["AGENTNET_LLM_API_KEY"] = str(llm["api_key"])
    if llm.get("model_id"):
        pairs["AGENTNET_LLM_MODEL"] = str(llm["model_id"])
    for k, v in pairs.items():
        os.environ.setdefault(k, v)
        if k in os.environ and os.environ[k] != v:
            logger.debug("env 已覆盖 configs.json 提供的 %s", k)


# 仅在模块加载时跑一次：configs.json → os.environ。
# 已经显式 export 过的环境变量不会被覆盖，保证部署期可调。
_apply_configs_env(_load_configs_json())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTNET_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "AgentNet"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    database_url: str = Field(
        default="postgresql+asyncpg://agentnet:agentnet@localhost:5432/agentnet"
    )

    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = "sk-replace-me"
    llm_model: str = "deepseek-chat"

    step_output_token_limit: int = 2048
    tiktoken_encoding: str = "cl100k_base"

    agent_rps_limit: int = 20
    breaker_window_sec: int = 10
    breaker_fail_threshold: int = 5
    breaker_open_sec: int = 60

    health_core_interval_sec: int = 30
    health_edge_interval_sec: int = 300
    health_probe_timeout_ms: int = 2000
    health_fail_threshold: int = 3
    health_core_idle_minutes: int = 60
    health_edge_idle_minutes: int = 1440


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _redact_key(s: str) -> str:
    """在日志里遮蔽 api_key 中段，避免意外泄露。"""
    if not s or len(s) < 8:
        return "***"
    return f"{s[:6]}...{s[-4:]}"


def log_effective_llm_config() -> None:
    """启动期打印当前生效的 LLM 配置（key 已遮蔽）。"""
    s = get_settings()
    logger.info(
        "LLM effective: base_url=%s model=%s api_key=%s",
        s.llm_base_url,
        s.llm_model,
        _redact_key(s.llm_api_key),
    )
