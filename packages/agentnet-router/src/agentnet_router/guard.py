"""出站安全策略:密钥拦截 + PII 脱敏。

远程 agent(以及网络中的任何第三方)不受信:问题发出去就收不回来。
- block_secrets:query 含密钥/凭证特征时永不外发(直接本地回答);
- redact_pii:外发前把邮箱/手机号/身份证号替换为占位符(可选,默认关)。
"""

from __future__ import annotations

import re

# (规则名, 模式) —— 常见密钥/凭证特征
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("OpenAI/兼容 API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("AWS Access Key", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_\-]{35}\b")),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{20,}", re.IGNORECASE)),
    ("密码赋值", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S{8,}")),
]

# (占位符, 模式) —— 中国场景常见 PII
PII_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("[邮箱]", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("[手机号]", re.compile(r"\b1[3-9]\d{9}\b")),
    ("[身份证号]", re.compile(r"\b\d{17}[\dXx]\b")),
]


def find_secret(text: str) -> str | None:
    """返回命中的密钥规则名;未命中返回 None。"""
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


def redact_pii(text: str) -> str:
    """把 PII 替换为占位符(仅用于外发副本,本地仍用原文)。"""
    for placeholder, pattern in PII_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text
