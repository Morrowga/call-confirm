"""Argon2 password hashing, JWT access/refresh tokens, opaque prefixed API keys.

API keys are `sk_live_...` / `sk_test_...` prefixed, opaque, generated once and
shown to the user a single time. Only a SHA-256 hash is stored; lookup is done
by hash, never by plaintext.
"""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_ph = PasswordHasher()

ROLE_BUSINESS_ADMIN = "business_admin"
ROLE_EVENT_ADMIN = "event_admin"
ROLE_PLATFORM_ADMIN = "platform_admin"


# --- Passwords -------------------------------------------------------------

def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False


# --- JWT -------------------------------------------------------------------

def _encode(payload: dict[str, Any], expires_delta: timedelta, token_type: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {**payload, "iat": now, "exp": now + expires_delta, "type": token_type}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(account_id: str, role: str) -> str:
    return _encode(
        {"sub": account_id, "role": role},
        timedelta(minutes=settings.access_token_minutes),
        "access",
    )


def create_refresh_token(account_id: str, role: str) -> str:
    return _encode(
        {"sub": account_id, "role": role},
        timedelta(days=settings.refresh_token_days),
        "refresh",
    )


def decode_token(token: str, expected_type: str = "access") -> dict[str, Any]:
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"Expected {expected_type} token")
    return payload


# --- API keys --------------------------------------------------------------

def generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, key_hash, key_prefix). Plaintext is never stored."""
    env = "live" if settings.is_production else "test"
    raw = f"sk_{env}_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw), raw[:12]


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


# --- One-time tokens (email verify, password reset, phone OTP) -------------

def generate_url_token() -> str:
    return secrets.token_urlsafe(48)


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"
