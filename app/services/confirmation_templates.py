"""Confirmation-type templates — universal base + niche-specific extras.

TEXT CONTENT (the actual per-language sentences) is now DB-backed and
admin-editable via the internal panel (see
app/api/internal/template_config.py), stored in PlatformConfig the same
way twilio_service.py's voice map is — self-seeded with
DEFAULT_TEMPLATE_TEXT the first time it's ever read, so the database
always holds the real live content after the first request, not just
"still hardcoded until someone happens to save an override."

STRUCTURAL metadata — detail_mode, has_scheduled_time, which
ConfirmationTypes exist, which industries get which extra types — stays
defined in code below. These are product/domain decisions (does this type
of confirmation have a schedule? does it need a detail slot filled in?),
not translated content, so they don't belong in the same admin-editable
text config.

Previously every template's text was English-only regardless of the
appointment's `language` field. Currently seeded with real Japanese text
for the five universal base types; niche-specific extras are English-only
seed data for now — "en" is the required fallback for any
language/type/field combination not yet translated, whether that's
because it was never in the original seed data or because no admin has
added it yet.
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ConfirmationType, Industry, PlatformConfig

DetailMode = str  # "required" | "optional" | "none"
_FALLBACK_LANG = "en"

TEMPLATE_TEXT_CONFIG_KEY = "confirmation_template_text"


def _localized(by_lang: dict[str, str], language: str) -> str:
    return by_lang.get(language, by_lang.get(_FALLBACK_LANG, ""))


class TemplateStructure:
    """Structural metadata only — no text. Stays code-defined; see module
    docstring for why this is separate from the DB-backed text content."""
    __slots__ = ("detail_mode", "has_scheduled_time")

    def __init__(self, detail_mode: DetailMode, has_scheduled_time: bool = True):
        self.detail_mode = detail_mode
        self.has_scheduled_time = has_scheduled_time


# --- Structural metadata — every ConfirmationType, code-defined ------------

_STRUCTURE: dict[ConfirmationType, TemplateStructure] = {
    ConfirmationType.appointment: TemplateStructure("optional"),
    ConfirmationType.reservation: TemplateStructure("optional"),
    ConfirmationType.meeting: TemplateStructure("optional"),
    ConfirmationType.order: TemplateStructure("required", has_scheduled_time=False),
    ConfirmationType.delivery: TemplateStructure("required"),
    ConfirmationType.property_viewing: TemplateStructure("required"),
    ConfirmationType.closing_appointment: TemplateStructure("required"),
    ConfirmationType.lease_signing: TemplateStructure("required"),
    ConfirmationType.legal_consultation: TemplateStructure("required"),
    ConfirmationType.deposition: TemplateStructure("none"),
    ConfirmationType.document_signing: TemplateStructure("required"),
    ConfirmationType.court_appearance: TemplateStructure("none"),
    ConfirmationType.jury_duty: TemplateStructure("none"),
    ConfirmationType.government_office: TemplateStructure("none"),
    ConfirmationType.visa_interview: TemplateStructure("none"),
    ConfirmationType.job_interview: TemplateStructure("optional"),
}

BASE_TYPES: list[ConfirmationType] = [
    ConfirmationType.appointment, ConfirmationType.reservation, ConfirmationType.meeting,
    ConfirmationType.order, ConfirmationType.delivery,
]

INDUSTRY_EXTRA_TYPES: dict[Industry, list[ConfirmationType]] = {
    Industry.real_estate: [
        ConfirmationType.property_viewing, ConfirmationType.closing_appointment,
        ConfirmationType.lease_signing, ConfirmationType.legal_consultation,
    ],
    Industry.law_firm: [
        ConfirmationType.legal_consultation, ConfirmationType.deposition,
        ConfirmationType.document_signing, ConfirmationType.court_appearance,
    ],
    Industry.government: [
        ConfirmationType.court_appearance, ConfirmationType.jury_duty,
        ConfirmationType.government_office, ConfirmationType.visa_interview,
    ],
    Industry.recruiting: [ConfirmationType.job_interview],
}


# --- DEFAULT text content — seed data only. Once seeded into the DB, this
# constant is never read again; admin edits via the internal panel become
# the live source of truth. Shape per type:
#   {"opening": {"en": "...", "ja": "..."},
#    "opening_with_detail": {...},   # optional key — falls back to "opening" if absent
#    "confirmed_closing": {...},
#    "declined_closing": {...}}

DEFAULT_TEMPLATE_TEXT: dict[str, dict[str, dict[str, str]]] = {
    "appointment": {
        "opening": {
            "en": "Hello {client_name}, you have an appointment with {business} at {time}.",
            "ja": "{client_name}様、{business}にて{time}にご予約がございます。",
        },
        "opening_with_detail": {
            "en": "Hello {client_name}, you have an appointment with {business} "
                  "regarding {subject_detail}, at {time}.",
            "ja": "{client_name}様、{business}にて{subject_detail}について{time}にご予約がございます。",
        },
        "confirmed_closing": {
            "en": "Thank you, you are confirmed. Goodbye.",
            "ja": "ありがとうございます。ご予約が確定いたしました。失礼いたします。",
        },
        "declined_closing": {
            "en": "Understood, this appointment has been cancelled. Goodbye.",
            "ja": "承知いたしました。こちらのご予約はキャンセルとなります。失礼いたします。",
        },
    },
    "reservation": {
        "opening": {
            "en": "Hello {client_name}, you have a reservation at {business} at {time}.",
            "ja": "{client_name}様、{business}にて{time}にご予約がございます。",
        },
        "opening_with_detail": {
            "en": "Hello {client_name}, you have a reservation at {business} for {subject_detail}, at {time}.",
            "ja": "{client_name}様、{business}にて{subject_detail}のご予約が{time}にございます。",
        },
        "confirmed_closing": {
            "en": "Perfect, we'll see you then. Goodbye.",
            "ja": "かしこまりました。お待ちしております。失礼いたします。",
        },
        "declined_closing": {
            "en": "Understood, your reservation has been cancelled. Goodbye.",
            "ja": "承知いたしました。こちらのご予約はキャンセルとなります。失礼いたします。",
        },
    },
    "meeting": {
        "opening": {
            "en": "Hello {client_name}, you have a meeting with {business} at {time}.",
            "ja": "{client_name}様、{business}との打ち合わせが{time}にございます。",
        },
        "opening_with_detail": {
            "en": "Hello {client_name}, you have a meeting with {business} regarding {subject_detail}, at {time}.",
            "ja": "{client_name}様、{business}との{subject_detail}に関する打ち合わせが{time}にございます。",
        },
        "confirmed_closing": {
            "en": "Thank you, you are confirmed. Goodbye.",
            "ja": "ありがとうございます。確定いたしました。失礼いたします。",
        },
        "declined_closing": {
            "en": "Understood, this meeting has been cancelled. Goodbye.",
            "ja": "承知いたしました。こちらの打ち合わせはキャンセルとなります。失礼いたします。",
        },
    },
    "order": {
        "opening": {
            "en": "Hello {client_name}, you have an order for {subject_detail} from {business}. "
                  "Would you like to confirm the order?",
            "ja": "{client_name}様、{business}より{subject_detail}のご注文を承っております。"
                  "ご注文を確定されますか。",
        },
        "confirmed_closing": {
            "en": "Great, we'll process your order. Goodbye.",
            "ja": "ありがとうございます。ご注文を確定いたします。失礼いたします。",
        },
        "declined_closing": {
            "en": "No problem, your order has been cancelled. Goodbye.",
            "ja": "承知いたしました。ご注文はキャンセルとなります。失礼いたします。",
        },
    },
    "delivery": {
        "opening": {
            "en": "Hello {client_name}, you have a delivery of {subject_detail} from {business} arriving at {time}.",
            "ja": "{client_name}様、{business}より{subject_detail}のお届けが{time}に予定されております。",
        },
        "confirmed_closing": {
            "en": "Thanks, we'll proceed with delivery. Goodbye.",
            "ja": "ありがとうございます。お届けを進めさせていただきます。失礼いたします。",
        },
        "declined_closing": {
            "en": "Understood, we'll hold off on delivery. Goodbye.",
            "ja": "承知いたしました。お届けを見合わせます。失礼いたします。",
        },
    },
    "property_viewing": {
        "opening": {
            "en": "Hello {client_name}, you have an appointment with {business} at {subject_detail} "
                  "to see the rooms, at {time}.",
        },
        "confirmed_closing": {"en": "Great, we'll see you then. Goodbye."},
        "declined_closing": {"en": "Understood, this viewing has been cancelled. Goodbye."},
    },
    "closing_appointment": {
        "opening": {"en": "Hello {client_name}, you have a closing appointment with {business} "
                          "for {subject_detail} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this closing has been cancelled. Goodbye."},
    },
    "lease_signing": {
        "opening": {"en": "Hello {client_name}, you have a lease signing with {business} "
                          "for {subject_detail} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this signing has been cancelled. Goodbye."},
    },
    "legal_consultation": {
        "opening": {"en": "Hello {client_name}, you have a consultation with {business} "
                          "regarding {subject_detail}, at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this consultation has been cancelled. Goodbye."},
    },
    "deposition": {
        "opening": {"en": "Hello {client_name}, you have a deposition scheduled with {business} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this has been cancelled. Goodbye."},
    },
    "document_signing": {
        "opening": {"en": "Hello {client_name}, you have a document signing with {business} "
                          "for {subject_detail} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this signing has been cancelled. Goodbye."},
    },
    "court_appearance": {
        "opening": {"en": "Hello {client_name}, you are required to appear at {business} at {time}. "
                          "This is a legal notice."},
        "confirmed_closing": {"en": "Thank you, your attendance is noted. Goodbye."},
        "declined_closing": {"en": "This notice has been acknowledged as cancelled. Goodbye."},
    },
    "jury_duty": {
        "opening": {"en": "Hello {client_name}, this is a reminder of your jury duty at {business} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this has been cancelled. Goodbye."},
    },
    "government_office": {
        "opening": {"en": "Hello {client_name}, you have an appointment at {business} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this appointment has been cancelled. Goodbye."},
    },
    "visa_interview": {
        "opening": {"en": "Hello {client_name}, you have a visa interview at {business} at {time}."},
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this has been cancelled. Goodbye."},
    },
    "job_interview": {
        "opening": {"en": "Hello {client_name}, you have an interview with {business} at {time}."},
        "opening_with_detail": {
            "en": "Hello {client_name}, you have an interview with {business} regarding {subject_detail}, at {time}.",
        },
        "confirmed_closing": {"en": "Thank you, you are confirmed. Goodbye."},
        "declined_closing": {"en": "Understood, this interview has been cancelled. Goodbye."},
    },
}


async def get_template_text(db: AsyncSession) -> dict[str, dict[str, dict[str, str]]]:
    """The live template text config — self-seeds DEFAULT_TEMPLATE_TEXT
    into PlatformConfig the first time this is ever called, same pattern
    as twilio_service.get_voice_map. After the first call, the database
    is always the real source of truth; DEFAULT_TEMPLATE_TEXT is never
    read again unless the admin explicitly resets it."""
    row = await db.get(PlatformConfig, TEMPLATE_TEXT_CONFIG_KEY)
    if row and row.value:
        return row.value
    if row is None:
        db.add(PlatformConfig(key=TEMPLATE_TEXT_CONFIG_KEY, value=DEFAULT_TEMPLATE_TEXT))
        await db.commit()
    return DEFAULT_TEMPLATE_TEXT


_JA_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]  # Python's weekday(): Mon=0 ... Sun=6


def format_scheduled_time(dt: datetime, language: str) -> str:
    """`dt` must already be converted to the appointment's own timezone
    before calling this — this only handles LOCALE formatting, not
    timezone conversion.

    Only en/ja have real localized phrasing here, matching the seed
    data's en/ja-only coverage — other languages fall back to the English
    format rather than showing raw English text as if it were untranslated
    filler."""
    if language == "ja":
        weekday = _JA_WEEKDAYS[dt.weekday()]
        period = "午前" if dt.hour < 12 else "午後"
        hour12 = dt.hour % 12 or 12
        return f"{dt.month}月{dt.day}日({weekday}) {period}{hour12}時{dt.minute:02d}分"
    return f"{dt:%A %B %d at %I:%M %p}"


def available_types(industry: Industry) -> list[ConfirmationType]:
    return BASE_TYPES + INDUSTRY_EXTRA_TYPES.get(industry, [])


async def render_opening(
    db: AsyncSession,
    confirmation_type: ConfirmationType, client_name: str, business: str,
    time_str: str, subject_detail: str | None, language: str = "en",
) -> str:
    text_config = await get_template_text(db)
    entry = text_config.get(confirmation_type.value, {})
    has_detail = bool((subject_detail or "").strip())
    structure = _STRUCTURE[confirmation_type]
    if has_detail and structure.detail_mode != "none" and "opening_with_detail" in entry:
        source = entry["opening_with_detail"]
    else:
        source = entry.get("opening", {})
    template_str = _localized(source, language)
    return template_str.format(
        client_name=client_name, business=business, time=time_str,
        subject_detail=subject_detail or "",
    )


async def render_closing(
    db: AsyncSession, confirmation_type: ConfirmationType, confirmed: bool, language: str = "en",
) -> str:
    text_config = await get_template_text(db)
    entry = text_config.get(confirmation_type.value, {})
    source = entry.get("confirmed_closing" if confirmed else "declined_closing", {})
    return _localized(source, language)


def detail_mode(confirmation_type: ConfirmationType) -> DetailMode:
    return _STRUCTURE[confirmation_type].detail_mode


def needs_subject_detail(confirmation_type: ConfirmationType) -> bool:
    return detail_mode(confirmation_type) == "required"


def has_scheduled_time(confirmation_type: ConfirmationType) -> bool:
    return _STRUCTURE[confirmation_type].has_scheduled_time


async def template_hint(db: AsyncSession, confirmation_type: ConfirmationType, language: str) -> str:
    """The raw template string with the {subject_detail} slot still visible
    — given to the CSV rephrasing AI so it can see exactly how the user's
    text gets inserted, and fix cases like a user typing their own "for"
    that duplicates the template's own connecting word."""
    text_config = await get_template_text(db)
    entry = text_config.get(confirmation_type.value, {})
    structure = _STRUCTURE[confirmation_type]
    source = entry.get("opening_with_detail" if structure.detail_mode != "none" else "opening", {})
    return _localized(source, language)