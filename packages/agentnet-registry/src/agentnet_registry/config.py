"""Registry 配置(AGENTNET_ 环境变量前缀)。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTNET_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://agentnet:agentnet@localhost:5432/agentnet"
    host: str = "0.0.0.0"
    port: int = 9000

    # 管理 key(启动时自动写入 api_keys 表,用于签发 provider/consumer key)
    admin_key: str = "changeme-admin-key"

    # embedding 后端:sentence-transformers(默认,本地 bge-m3)| hash(无模型环境/测试)
    embedding_backend: str = "sentence-transformers"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024

    recall_top_k: int = 10

    agent_timeout_sec: float = 30.0  # 普通协议端点超时
    gateway_timeout_sec: float = 300.0  # SSE 事件流超时

    rate_limit_rps: int = 20
    breaker_fail_threshold: int = 5
    breaker_open_sec: float = 60.0
    breaker_window_sec: float = 10.0

    inspect_enabled: bool = True
    inspect_interval_sec: float = 60.0
    inspect_fail_threshold: int = 3

    canary_enabled: bool = True
    canary_interval_sec: float = 300.0
    canary_timeout_sec: float = 30.0
