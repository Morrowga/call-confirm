"""Registration & auth for the Business/Event admin panels.

Multi-step wizard (not one long form):
  step 1: email + phone + password  ->  step 2: name/country/timezone/language
  -> email confirmation link -> phone OTP -> account enters Demo Mode.

No third-party social login — email/phone verification only, both account types.
"""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    ROLE_BUSINESS_ADMIN, ROLE_EVENT_ADMIN, ROLE_PLATFORM_ADMIN,
    create_access_token, create_refresh_token, decode_token, generate_otp,
    generate_url_token, hash_password, verify_password,
)
from app.models import BusinessAccount, EventAccount, PlatformAdmin, VerificationToken
from app.schemas.common import (
    ForgotPasswordRequest, LoginRequest, RefreshRequest, RegistrationStep1,
    RegistrationStep2, ResetPasswordRequest, TokenPair, VerifyEmailRequest,
    VerifyPhoneRequest,
)
from app.services import notifications

router = APIRouter(prefix="/auth", tags=["company:auth"])

_MODEL = {"business": BusinessAccount, "event": EventAccount}
_ROLE = {"business": ROLE_BUSINESS_ADMIN, "event": ROLE_EVENT_ADMIN}

def _reject_event_if_disabled(account_type: str) -> None:
    """Launch scope gate — see settings.events_enabled. Called at every
    entry point that accepts a caller-supplied account_type, so an Event
    account (new registration, or a pre-existing one like the dev test
    account) can never obtain a fresh access/refresh token while disabled.
    Every other event-owned route requires that token, so this is a
    complete block, not just a UI-level hide."""
    if account_type == "event" and not settings.events_enabled:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Event accounts are not available right now. Please check back later.",
        )

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _issue_token(db, account_type: str, account_id: uuid.UUID, purpose: str, ttl: timedelta) -> str:
    raw = generate_otp() if purpose == "phone_otp" else generate_url_token()
    db.add(VerificationToken(
        account_type=account_type, account_id=account_id, purpose=purpose,
        token_hash=_hash_token(raw), expires_at=datetime.now(timezone.utc) + ttl,
    ))
    await db.commit()
    return raw


async def _consume_token(db, purpose: str, raw: str) -> VerificationToken:
    row = (
        await db.execute(
            select(VerificationToken).where(
                VerificationToken.purpose == purpose,
                VerificationToken.token_hash == _hash_token(raw),
                VerificationToken.used_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None or row.expires_at < datetime.now(timezone.utc):
        what = "verification link" if purpose == "email_verify" else "verification code"
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"This {what} is invalid or has expired. Please request a new one.",
        )
    row.used_at = datetime.now(timezone.utc)
    return row


@router.post("/register/step1", status_code=201)
async def register_step1(body: RegistrationStep1, db: AsyncSession = Depends(get_db)):
    _reject_event_if_disabled(body.account_type)
    model = _MODEL[body.account_type]
    exists = (await db.execute(select(model).where(model.email == body.email))).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    account = model(
        email=body.email,
        phone_number=body.phone_number,
        password_hash=hash_password(body.password),
        name="", country="XX",
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return {"account_id": str(account.id), "next": "step2"}


@router.post("/register/step2")
async def register_step2(body: RegistrationStep2, db: AsyncSession = Depends(get_db)):
    _reject_event_if_disabled(body.account_type)
    account = await db.get(_MODEL[body.account_type], body.account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    account.name = body.name
    account.country = body.country.upper()
    account.preferred_language = body.preferred_language
    if body.account_type == "business":
        account.timezone = body.timezone
    await db.commit()

    email_token = await _issue_token(db, body.account_type, account.id, "email_verify", timedelta(days=2))
    otp = await _issue_token(db, body.account_type, account.id, "phone_otp", timedelta(minutes=10))
    notifications.send_email(
        account.email, "email_verify",
        link=f"{settings.api_base_url}/verify-email?token={email_token}",
    )
    notifications.send_otp_sms(account.phone_number, otp)
    return {"next": "verify_email_and_phone"}


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    token = await _consume_token(db, "email_verify", body.token)
    account = await db.get(_MODEL[token.account_type], token.account_id)
    account.email_verified = True
    await db.commit()
    return {"email_verified": True}


@router.post("/verify-phone")
async def verify_phone(body: VerifyPhoneRequest, db: AsyncSession = Depends(get_db)):
    token = await _consume_token(db, "phone_otp", body.otp)
    if token.account_id != body.account_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This verification code does not belong to this account.")
    account = await db.get(_MODEL[body.account_type], body.account_id)
    account.phone_verified = True
    await db.commit()
    return {"phone_verified": True, "demo_mode": True}


@router.post("/login", response_model=TokenPair)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    _reject_event_if_disabled(body.account_type)
    if body.account_type == "platform_admin":
        admin = (
            await db.execute(select(PlatformAdmin).where(PlatformAdmin.email == body.email))
        ).scalar_one_or_none()
        if admin is None or not verify_password(body.password, admin.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
        return TokenPair(
            access_token=create_access_token(str(admin.id), ROLE_PLATFORM_ADMIN),
            refresh_token=create_refresh_token(str(admin.id), ROLE_PLATFORM_ADMIN),
        )
    model = _MODEL[body.account_type]
    account = (await db.execute(select(model).where(model.email == body.email))).scalar_one_or_none()
    if account is None or not verify_password(body.password, account.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    role = _ROLE[body.account_type]
    return TokenPair(
        access_token=create_access_token(str(account.id), role),
        refresh_token=create_refresh_token(str(account.id), role),
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest):
    try:
        payload = decode_token(body.refresh_token, "refresh")
    except pyjwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    return TokenPair(
        access_token=create_access_token(payload["sub"], payload["role"]),
        refresh_token=create_refresh_token(payload["sub"], payload["role"]),
    )


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    _reject_event_if_disabled(body.account_type)
    model = _MODEL[body.account_type]
    account = (await db.execute(select(model).where(model.email == body.email))).scalar_one_or_none()
    if account:  # never reveal whether the email exists
        raw = await _issue_token(
            db, body.account_type, account.id, "password_reset",
            timedelta(hours=settings.password_reset_token_hours),
        )
        notifications.send_email(
            account.email, "password_reset",
            link=f"{settings.api_base_url}/reset-password?token={raw}",
        )
    return {"sent": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    token = await _consume_token(db, "password_reset", body.token)
    account = await db.get(_MODEL[token.account_type], token.account_id)
    account.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"reset": True}