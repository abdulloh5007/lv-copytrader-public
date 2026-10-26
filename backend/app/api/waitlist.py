"""Public waitlist signup; emails are encrypted, never returned or logged."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, func, select
from sqlalchemy.dialects.sqlite import insert

from app.config import settings
from app.crypto import encrypt
from app.db import session_scope
from app.models import WaitlistEntry, WaitlistThrottle
from app.util.auth import resolve_secret

router = APIRouter(prefix="/api/waitlist", tags=["waitlist"])
EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


class Signup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    consent: Literal[True]
    website: str = Field(default="", max_length=200)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        value = value.strip().lower()
        local = value.partition("@")[0]
        if not EMAIL.fullmatch(value) or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
            raise ValueError("Enter a valid email address")
        return value


def digest(value: str) -> str:
    return hmac.new(resolve_secret(settings.SECRET_KEY).encode(), value.encode(), hashlib.sha256).hexdigest()


@router.post("", status_code=200)
async def join(payload: Signup, request: Request) -> dict[str, bool]:
    # Honeypots get the same response, with no stored address.
    if payload.website:
        return {"ok": True}
    now = int(time.time())
    window = now // 60
    peer = request.client.host if request.client else "unknown"
    async with session_scope() as session:
        statement = insert(WaitlistThrottle).values(key=digest("peer:" + peer), window=window, attempts=1)
        statement = statement.on_conflict_do_update(
            index_elements=[WaitlistThrottle.key],
            set_={"window": window, "attempts": case((WaitlistThrottle.window == window, WaitlistThrottle.attempts + 1), else_=1)},
        ).returning(WaitlistThrottle.attempts)
        attempts = (await session.execute(statement)).scalar_one()
        # Commit the throttle even for rejected requests; rollback would erase it.
        await session.execute(WaitlistThrottle.__table__.delete().where(WaitlistThrottle.window < window - 60))
    if attempts > 20:
        raise HTTPException(429, "Too many requests. Try again in a minute.", headers={"Retry-After": str(60 - now % 60)})
    async with session_scope() as session:
        await session.execute(insert(WaitlistEntry).values(
            email_hash=digest("email:" + payload.email), email_encrypted=encrypt(payload.email),
            consent_version="2026-10-21",
        ).on_conflict_do_nothing(index_elements=[WaitlistEntry.email_hash]))
    # Identical for existing and new entries to prevent email enumeration.
    return {"ok": True}


@router.get("")
async def overview() -> dict[str, int]:
    """Protected by AuthMiddleware; public exemption applies to POST only."""
    async with session_scope() as session:
        return {"count": int((await session.scalar(select(func.count()).select_from(WaitlistEntry))) or 0)}
