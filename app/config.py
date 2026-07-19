"""AgentNet 配置文件，使用 pydantic-settings 从环境加载。"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
