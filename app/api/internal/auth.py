"""Platform admin login — separate from company panel auth
(app/api/company/auth.py) since PlatformAdmin is a completely distinct
table/role, not just another BusinessAccount/EventAccount. Uses the same
TokenPair/JWT pattern as the rest of the app (see app/core/security.py)
for consistency. No public signup exists for admins by design — the
first admin row must be created directly; see the bootstrap instructions
alongside this file's deployment notes.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import (
    ROLE_PLATFORM_ADMIN, create_access_token, create_refresh_token, decode_token, verify_password,
)
from app.models import PlatformAdmin

router = APIRouter(prefix="/auth", tags=["internal:auth"])


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class AdminRefreshRequest(BaseModel):
    refresh_token: str


class AdminTokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


@router.post("/login", response_model=AdminTokenPair)
async def admin_login(body: AdminLoginRequest, db: AsyncSession = Depends(get_db)):
    admin = (
        await db.execute(select(PlatformAdmin).where(PlatformAdmin.email == body.email))
    ).scalar_one_or_none()
    # Same-message failure for "no such admin" and "wrong password" —
    # never reveal which one it was, standard login-enumeration hygiene.
    if admin is None or not verify_password(body.password, admin.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    return AdminTokenPair(
        access_token=create_access_token(str(admin.id), ROLE_PLATFORM_ADMIN),
        refresh_token=create_refresh_token(str(admin.id), ROLE_PLATFORM_ADMIN),
    )


@router.post("/refresh", response_model=AdminTokenPair)
async def admin_refresh(body: AdminRefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        payload = decode_token(body.refresh_token, "refresh")
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    if payload.get("role") != ROLE_PLATFORM_ADMIN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    admin = await db.get(PlatformAdmin, payload["sub"])
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired refresh token")
    return AdminTokenPair(
        access_token=create_access_token(str(admin.id), ROLE_PLATFORM_ADMIN),
        refresh_token=create_refresh_token(str(admin.id), ROLE_PLATFORM_ADMIN),
    )