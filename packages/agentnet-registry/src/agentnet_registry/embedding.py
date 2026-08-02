"""Embedding 服务。

- SentenceTransformerEmbedding:本地 bge-m3,惰性加载,线程池执行避免阻塞事件循环。
- HashEmbedding:无模型环境/测试用。字符 n-gram 哈希向量,语义粗糙但
  有真实的余弦相似度信号(共享 token 越多越相似),且完全确定,适合测试。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Protocol

from agentnet_core import AgentCard

from .config import Settings


class Embedder(Protocol):
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedding:
    def __init__(self, model_name: str, dim: int) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model = None

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(self.model_name)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vecs = self._model.encode(list(texts), normalize_embeddings=True)
        return [v.tolist() for v in vecs]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            await asyncio.to_thread(self._load)
        return await asyncio.to_thread(self._encode, texts)


class HashEmbedding:
    """确定性哈希向量(测试/无模型环境验证管线用,不做生产语义匹配)。"""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        text = text.lower()
        tokens = text.split()
        tokens += [text[i : i + 3] for i in range(max(0, len(text) - 2))]
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            vec[int.from_bytes(digest, "little") % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_backend == "hash":
        return HashEmbedding(dim=settings.embedding_dim)
    return SentenceTransformerEmbedding(settings.embedding_model, dim=settings.embedding_dim)


def card_embed_text(card: AgentCard) -> str:
    """Agent Card 的 embedding 输入文本。"""
    return "\n".join(
        part
        for part in [
            card.name,
            card.description,
            card.natural_capabilities,
            " ".join(card.capabilities),
        ]
        if part
    )
