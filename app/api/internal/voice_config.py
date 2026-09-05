"""Platform admin: manage which Twilio voice each language uses — the
admin-editable replacement for what used to be a hardcoded dict in
twilio_service.py. Stored in PlatformConfig (same JSONB runtime-config
table already used for auto-purchase caps), not a dedicated table — this
is a single small config blob, not relational data.

Also manages supported_languages — the launch-time subset of
VALID_LANGUAGES actually offered to users at registration/Settings (see
the new public /meta/registration-options endpoint). Kept here rather
than a separate file since it's the same "language admin surface"
conceptually, just controlling which languages are OFFERED rather than
what voice each one uses.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_platform_admin
from app.models import PlatformConfig
from app.services.twilio_service import DEFAULT_VOICE_MAP, VOICE_CONFIG_KEY, get_voice_map

router = APIRouter(prefix="/voice-config", tags=["internal:voice-config"])

SUPPORTED_LANGUAGES_KEY = "supported_languages"
# Launch set matching the initial 5 supported countries (Myanmar, US, UK,
# Japan, Thailand) — en covers both US and UK, ja covers Japan, th covers
# Thailand. Myanmar has NO entry here: Burmese ("my") does not exist in
# app/core/languages.py's VALID_LANGUAGES at all yet — add it there first
# if Myanmar accounts should get native-language calls; until then they
# fall back to English like any other unsupported language does
# elsewhere in this codebase.
DEFAULT_SUPPORTED_LANGUAGES: list[str] = ["en", "en-GB", "ja"]

# Cross-referenced directly against Amazon Polly's official documented
# language list (docs.aws.amazon.com/polly/latest/dg/supported-languages.html)
# — must stay in sync with the frontend's identical
# src/lib/twilio-supported-languages.ts. Confirmed NOT supported by
# Polly: vi, th, id, ms, fil, bn, ta, te, mr, gu, pa, kn, ml, my
# (Myanmar/Burmese) — these would need a third-party TTS integration,
# not just a config change.
TWILIO_SUPPORTED_LANGUAGES: set[str] = {
    "en", "en-GB", "en-AU", "en-IN",
    "es", "es-MX",
    "fr", "fr-CA",
    "de", "it", "pt", "pt-BR", "ru", "pl", "tr", "sv", "nb", "da", "fi",
    "hi", "ar", "zh", "ja", "ko",
}


@router.get("")
async def get_config(_admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    """Current live map (admin override if one exists, otherwise the
    built-in default) plus whether it's actually been customized yet."""
    row = await db.get(PlatformConfig, VOICE_CONFIG_KEY)
    return {
        "voice_map": await get_voice_map(db),
        "is_customized": row is not None and bool(row.value),
        "default_voice_map": DEFAULT_VOICE_MAP,
    }


@router.put("")
async def set_config(
    voice_map: dict[str, list[str]],
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Replaces the ENTIRE map. Body: {"en": ["Polly.Joanna", "Polly.Joanna-Neural"], ...}
    — each value is [standard_voice, neural_voice]. To add or edit one
    language, fetch the current map via GET first, modify it client-side,
    and PUT the whole thing back — same pattern as
    /number-pool/auto-purchase-caps.

    Every key must already be in supported_languages — a voice mapping
    for a language nobody can even select doesn't mean anything (see
    module docstring). This was previously only a soft suggestion in the
    frontend's picker (it only offered supported languages when ADDING a
    new row), not an actual enforced rule — someone editing an existing
    row, or calling this endpoint directly, could still submit an
    orphaned entry. Real 400 here instead."""
    supported = set(await get_supported_languages(db))
    unsupported = [lang for lang in voice_map if lang not in supported]
    if unsupported:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"These languages aren't in supported_languages yet: {', '.join(unsupported)}. "
            "Add them there first, or remove them from the voice map.",
        )
    row = await db.get(PlatformConfig, VOICE_CONFIG_KEY)
    if row:
        row.value = voice_map
    else:
        db.add(PlatformConfig(key=VOICE_CONFIG_KEY, value=voice_map))
    await db.commit()
    return {"saved": voice_map}


@router.post("/reset")
async def reset_to_default(
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Deletes the admin override entirely, reverting to
    DEFAULT_VOICE_MAP (the original hardcoded en/ja/etc. set)."""
    row = await db.get(PlatformConfig, VOICE_CONFIG_KEY)
    if row:
        await db.delete(row)
        await db.commit()
    return {"voice_map": DEFAULT_VOICE_MAP}


async def get_supported_languages(db: AsyncSession) -> list[str]:
    """Self-seeds DEFAULT_SUPPORTED_LANGUAGES the first time this is ever
    read, same pattern as get_voice_map — the database holds the real
    live list after the first call, not just a hardcoded fallback. Also
    used directly by the public /meta/registration-options endpoint."""
    row = await db.get(PlatformConfig, SUPPORTED_LANGUAGES_KEY)
    if row and row.value:
        return row.value
    if row is None:
        db.add(PlatformConfig(key=SUPPORTED_LANGUAGES_KEY, value=DEFAULT_SUPPORTED_LANGUAGES))
        await db.commit()
    return DEFAULT_SUPPORTED_LANGUAGES


@router.get("/supported-languages")
async def get_supported_languages_endpoint(
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    return {"supported_languages": await get_supported_languages(db)}


@router.put("/supported-languages")
async def set_supported_languages(
    languages: list[str],
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Body: ["en", "en-GB", "ja", ...] — a plain list, replaces the
    whole set.

    Validated against TWILIO_SUPPORTED_LANGUAGES, cross-referenced
    directly against Amazon Polly's real documented language list (see
    that constant's docstring). This used to have no validation at all
    ("an admin adding a code that isn't real is a mistake worth letting
    them see"), which is exactly what let Thai and Vietnamese sit marked
    "supported" for a while with fake, mismatched voice mappings
    (Polly.Amy — British English — assigned to Thai) — the mistake
    wasn't visible at all until specifically investigated, not
    "immediately seen" as assumed. Real validation now."""
    unsupported = [lang for lang in languages if lang not in TWILIO_SUPPORTED_LANGUAGES]
    if unsupported:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Twilio doesn't have a native voice for: {', '.join(unsupported)}. "
            "These would need a third-party TTS integration first, not a config change.",
        )
    row = await db.get(PlatformConfig, SUPPORTED_LANGUAGES_KEY)
    if row:
        row.value = languages
    else:
        db.add(PlatformConfig(key=SUPPORTED_LANGUAGES_KEY, value=languages))
    await db.commit()
    return {"saved": languages}