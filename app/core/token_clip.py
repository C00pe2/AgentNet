"""BPE Token 长度硬截断。

SPEC §3.3 要求精确按 token 数截断 (默认 2048 tokens)，尾部追加截断说明。
我们用 tiktoken 的 BPE 编码 (默认 cl100k_base，与主流中文 LLM 兼容) 来精确计量 token。
"""
from __future__ import annotations

import threading

import tiktoken

from app.config import get_settings

_TRIM_NOTICE_ZH = "[... 内容因超出网关 2K Token 限制已被截断 ...]"
_TRIM_NOTICE_EN = "[... content trimmed due to 2K token gateway cap ...]"

_ENCODER_LOCK = threading.Lock()
_ENCODERS: dict[str, "tiktoken.Encoding"] = {}


def _get_encoder(name: str) -> "tiktoken.Encoding":
    with _ENCODER_LOCK:
        enc = _ENCODERS.get(name)
        if enc is None:
            enc = tiktoken.get_encoding(name)
            _ENCODERS[name] = enc
        return enc


def clip_text_to_token_budget(
    text: str,
    *,
    token_limit: int | None = None,
    encoding: str | None = None,
) -> str:
    """按 token 预算硬截断。

    1) token_limit 默认取自配置 (AGENTNET_STEP_OUTPUT_TOKEN_LIMIT=2048)。
    2) 编码默认取 AGENTNET_TIKTOKEN_ENCODING (cl100k_base)。
    3) 超限部分直接截断，并在尾部追加截断说明。
    4) 截断说明以中文为主 (符合内网惯例)；如果原始文本是英文主导，会自动切换为英文版本。
    """
    if text is None:
        return ""
    settings = get_settings()
    limit = token_limit if token_limit is not None else settings.step_output_token_limit
    enc_name = encoding or settings.tiktoken_encoding
    enc = _get_encoder(enc_name)

    tokens = enc.encode(text)
    if len(tokens) <= limit:
        return text

    kept = enc.decode(tokens[:limit]).rstrip()
    notice = _TRIM_NOTICE_EN if _looks_english(text) else _TRIM_NOTICE_ZH
    return kept + notice


def _looks_english(text: str) -> bool:
    if not text:
        return False
    ascii_letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return ascii_letters > len(text) * 0.6
