"""AgentNet consumer router."""

from .client import Candidate, RegistryClient, RegistryError
from .llm import RerankResult, build_rerank_prompt, parse_rerank_response, rerank
from .router import AskResult, RouteDecision, Router
from .settings import LLMSettings, RouterSettings

__version__ = "0.1.0"

__all__ = [
    "AskResult",
    "Candidate",
    "LLMSettings",
    "RegistryClient",
    "RegistryError",
    "RerankResult",
    "RouteDecision",
    "Router",
    "RouterSettings",
    "build_rerank_prompt",
    "parse_rerank_response",
    "rerank",
]
