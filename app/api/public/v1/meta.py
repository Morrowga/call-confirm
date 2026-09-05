"""Public, unauthenticated — the live, admin-configured lists of what
this platform actually supports right now: which countries can get a
real number, and which call languages are offered. Used by registration
(Step 2's country/language pickers) and Settings' "Call language" card,
so the frontend never hardcodes a list that can silently drift out of
sync with what the backend can actually fulfill.

Countries come from the SAME per_country keys the Number Pool admin
page's auto-purchase caps editor manages (see number_provisioning.py's
_auto_purchase_caps / PlatformConfig key "number_auto_purchase_caps") —
a country isn't just "capped" by being listed there, it's the ONLY thing
that makes it purchasable/selectable at all. Languages come from
voice_config.py's supported_languages (a curated subset of the full
VALID_LANGUAGES set).
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models import PlatformConfig

router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/registration-options")
async def registration_options(db: AsyncSession = Depends(get_db)):
    from app.api.internal.voice_config import get_supported_languages

    caps_row = await db.get(PlatformConfig, "number_auto_purchase_caps")
    countries = sorted((caps_row.value.get("per_country", {}) if caps_row else {}).keys())
    languages = await get_supported_languages(db)
    return {"countries": countries, "languages": languages}