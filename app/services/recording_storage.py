"""Voice message recording storage — local disk in test/dev, S3 in
production. Same environment-driven split already used for Twilio/Stripe
credentials (settings.is_production), so local testing never silently
depends on real AWS access. Previously the recording webhook called boto3
directly inside a bare try/except that swallowed any failure — meaning a
missing/invalid AWS credential locally just silently discarded the audio
while still recording metadata as if it had succeeded. This module makes
the storage backend an explicit, correct choice instead.
"""
from pathlib import Path

import boto3

from app.core.config import settings

_PRESIGNED_URL_EXPIRY_SECONDS = 3600  # 1 hour — long enough to actually listen/download, not indefinite


def _s3_client():
    return boto3.client("s3", region_name=settings.aws_region)


def _local_path(key: str) -> Path:
    return Path(settings.local_recordings_dir) / key


def save_recording(key: str, audio_bytes: bytes) -> None:
    """`key` is a relative path like "recordings/{call_id}.mp3" — the same
    format used for both backends, so nothing else needs to change based on
    which one is active."""
    if settings.is_production:
        _s3_client().put_object(Bucket=settings.s3_bucket, Key=key, Body=audio_bytes)
    else:
        path = _local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio_bytes)


def read_recording(key: str) -> bytes | None:
    """Reads the recording from whichever backend is active — used by the
    retention task's export-before-delete step, which previously called
    boto3 directly and would have silently mis-behaved against local-stored
    recordings (deleting a key that was never actually in S3, while the
    real local file remained forever)."""
    if settings.is_production:
        try:
            obj = _s3_client().get_object(Bucket=settings.s3_bucket, Key=key)
            return obj["Body"].read()
        except Exception:
            return None
    return read_local_recording(key)


def delete_recording(key: str) -> None:
    if settings.is_production:
        _s3_client().delete_object(Bucket=settings.s3_bucket, Key=key)
    else:
        path = _local_path(key)
        path.unlink(missing_ok=True)


def read_local_recording(key: str) -> bytes | None:
    """Local/test only — used by the authenticated serving endpoint (see
    dashboard.py's /calls/{call_id}/recording), since local files have no
    equivalent to an S3 presigned URL that can be handed straight to the
    frontend."""
    path = _local_path(key)
    if not path.exists():
        return None
    return path.read_bytes()


def recording_exists(key: str) -> bool:
    if settings.is_production:
        try:
            _s3_client().head_object(Bucket=settings.s3_bucket, Key=key)
            return True
        except Exception:
            return False
    return _local_path(key).exists()


def recording_url(key: str, call_id: str) -> str | None:
    """Production: a short-lived presigned S3 URL (no auth needed for the
    URL itself — the signature IS the authorization, matching how S3
    presigned URLs normally work). Local/test: routed through our own
    backend instead, which re-verifies the requesting account actually owns
    this call before serving the file — local disk has no equivalent
    built-in access control, so this endpoint provides it. Returns None if
    the recording doesn't actually exist (e.g. never uploaded, or past
    retention and deleted) rather than a URL that would 404."""
    if not recording_exists(key):
        return None
    if settings.is_production:
        return _s3_client().generate_presigned_url(
            "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=_PRESIGNED_URL_EXPIRY_SECONDS,
        )
    # Relative, not absolute — Vite's dev server proxies /api/... calls to
    # the backend so local dev never needs CORS configured (see
    # vite.config.ts). An absolute URL bypasses that proxy entirely, making
    # this a genuine cross-origin request the backend has no CORS
    # middleware to handle — confirmed via testing (blocked preflight,
    # ERR_FAILED). A relative path goes through the exact same proxy every
    # other API call already uses, with zero special-casing needed.
    return f"/api/company/dashboard/calls/{call_id}/recording"