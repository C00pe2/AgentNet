"""API Key 鉴权:签发、校验、角色依赖。

Key 明文只在签发时返回一次,库里只存 sha256。
角色:admin(签发 key)/ provider(注册管理 agent)/ consumer(搜索与调用)。
"""

from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from .db import ApiKeyRow, get_sessionmaker


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key(role: str) -> str:
    return f"an_{role}_{secrets.token_urlsafe(24)}"


class Principal(BaseModel):
    role: str
    name: str


async def ensure_admin_key(admin_key: str) -> None:
    """启动时保证 admin key 存在。"""
    sm = get_sessionmaker()
    async with sm() as session:
        exists = await session.scalar(
            select(ApiKeyRow.id).where(ApiKeyRow.key_hash == hash_key(admin_key))
        )
        if not exists:
            session.add(ApiKeyRow(key_hash=hash_key(admin_key), role="admin", name="admin"))
            await session.commit()


async def get_principal(
    request: Request, authorization: str | None = Header(None)
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <api_key>")
    key = authorization.removeprefix("Bearer ").strip()
    sm = request.app.state.session_maker
    async with sm() as session:
        row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.key_hash == hash_key(key)))
    if row is None or not row.active:
        raise HTTPException(401, "API key 无效或已停用")
    return Principal(role=row.role, name=row.name)


def require_role(*roles: str):
    async def _dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(403, f"需要 {' / '.join(roles)} 角色")
        return p

    return _dep


require_admin = require_role("admin")
require_provider = require_role("provider")
require_consumer = require_role("consumer")
