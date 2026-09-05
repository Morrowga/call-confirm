"""FastAPI auth dependencies.

Two distinct systems:
  * Panel auth (business/event owners + platform admin): short-lived JWT.
  * External API auth (integrators): opaque hashed API keys with scopes.

Authorization is enforced here at the API layer: account users can only reach
their own rows, and platform-admin routes explicitly re-check the role on
every request.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import jwt as pyjwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    ROLE_BUSINESS_ADMIN, ROLE_EVENT_ADMIN, ROLE_PLATFORM_ADMIN,
    decode_token, hash_api_key,
)
from app.models import ApiKey, BusinessAccount, EventAccount, PlatformAdmin, SubscriptionTier

bearer = HTTPBearer(auto_error=False)


@dataclass
class AuthContext:
    role: str
    account_id: uuid.UUID


async def _jwt_context(creds: HTTPAuthorizationCredentials | None) -> AuthContext:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        payload = decode_token(creds.credentials, "access")
    except pyjwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired or is invalid. Please log in again.")
    return AuthContext(role=payload["role"], account_id=uuid.UUID(payload["sub"]))


async def current_business_account(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> BusinessAccount:
    ctx = await _jwt_context(creds)
    if ctx.role != ROLE_BUSINESS_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Business account required")
    account = await db.get(BusinessAccount, ctx.account_id)
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    return account


async def current_event_account(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> EventAccount:
    ctx = await _jwt_context(creds)
    if ctx.role != ROLE_EVENT_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Event account required")
    account = await db.get(EventAccount, ctx.account_id)
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    return account


async def current_panel_account(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> BusinessAccount | EventAccount:
    """Either owner type, for shared company-panel routes."""
    ctx = await _jwt_context(creds)
    if ctx.role == ROLE_BUSINESS_ADMIN:
        account = await db.get(BusinessAccount, ctx.account_id)
    elif ctx.role == ROLE_EVENT_ADMIN:
        account = await db.get(EventAccount, ctx.account_id)
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account owner required")
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    return account


async def require_platform_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> PlatformAdmin:
    """Explicit platform_admin role check — 'authenticated' never implies
    'authorized' for internal routes."""
    ctx = await _jwt_context(creds)
    if ctx.role != ROLE_PLATFORM_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin only")
    admin = await db.get(PlatformAdmin, ctx.account_id)
    if admin is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin only")
    return admin


def require_api_key(*required_scopes: str):
    """External API auth. API access is a paid tier — the owning account must
    be on the `api` subscription tier and active."""

    async def dependency(
        authorization: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> BusinessAccount:
        if not authorization or not authorization.startswith("Bearer sk_"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API key required")
        raw = authorization.removeprefix("Bearer ").strip()
        row = (
            await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw)))
        ).scalar_one_or_none()
        if row is None or row.revoked:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
        missing = [s for s in required_scopes if s not in (row.scopes or [])]
        if missing:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"API key missing scopes: {', '.join(missing)}"
            )
        account = await db.get(BusinessAccount, row.business_account_id)
        if account is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
        if account.subscription_tier != SubscriptionTier.api:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "API access is a paid tier")
        if account.status.value not in ("active",):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Account is {account.status.value}")
        row.last_used_at = datetime.now(timezone.utc)
        await db.commit()
        return account

    return dependency