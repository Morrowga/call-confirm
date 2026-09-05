"""Platform admin: manage confirmation-call template TEXT per language —
the admin-editable replacement for what used to be hardcoded Python
strings in confirmation_templates.py. Stored in PlatformConfig (same
JSONB runtime-config table voice_config.py uses), self-seeded on first
read — see confirmation_templates.get_template_text.

Structural metadata (detail_mode, has_scheduled_time, which types exist,
which industries get which extras) is NOT editable here — that's a
product/domain decision defined in code, not translated content. This
endpoint only ever touches the text.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_platform_admin
from app.models import ConfirmationType, PlatformConfig
from app.services.confirmation_templates import (
    DEFAULT_TEMPLATE_TEXT, TEMPLATE_TEXT_CONFIG_KEY, get_template_text,
)

router = APIRouter(prefix="/template-config", tags=["internal:templates"])


@router.get("")
async def get_config(_admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db)):
    """Current live text config, plus the full list of valid
    ConfirmationType keys — so the admin UI knows exactly which types it
    can show/edit fields for, without hardcoding that list separately on
    the frontend too."""
    row = await db.get(PlatformConfig, TEMPLATE_TEXT_CONFIG_KEY)
    return {
        "template_text": await get_template_text(db),
        "is_customized": row is not None and bool(row.value),
        "default_template_text": DEFAULT_TEMPLATE_TEXT,
        "confirmation_types": [t.value for t in ConfirmationType],
    }


@router.put("")
async def set_config(
    template_text: dict[str, dict[str, dict[str, str]]],
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Replaces the ENTIRE text config. Body shape:
    {"appointment": {"opening": {"en": "...", "ja": "..."},
                      "opening_with_detail": {...},
                      "confirmed_closing": {...},
                      "declined_closing": {...}},
     ...}
    To edit one type/field/language, fetch the current config via GET
    first, modify it client-side, and PUT the whole thing back — same
    pattern as voice-config and number-pool's auto-purchase-caps."""
    row = await db.get(PlatformConfig, TEMPLATE_TEXT_CONFIG_KEY)
    if row:
        row.value = template_text
    else:
        db.add(PlatformConfig(key=TEMPLATE_TEXT_CONFIG_KEY, value=template_text))
    await db.commit()
    return {"saved": template_text}


@router.post("/reset")
async def reset_to_default(
    _admin=Depends(require_platform_admin), db: AsyncSession = Depends(get_db),
):
    """Deletes the admin override entirely, reverting to
    DEFAULT_TEMPLATE_TEXT (the original seed content)."""
    row = await db.get(PlatformConfig, TEMPLATE_TEXT_CONFIG_KEY)
    if row:
        await db.delete(row)
        await db.commit()
    return {"template_text": DEFAULT_TEMPLATE_TEXT}