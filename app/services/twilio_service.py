"""Twilio integration.

Credentials are resolved from settings per APP_ENV — test and production use
fully separate accounts/keys. Every Call row records which environment it was
placed from.

Voice tiers: Business accounts get Neural TTS (Amazon Polly Neural voices via
Twilio), Event accounts get Standard tier. Language is passed per call —
appointments always carry their own per-row language; campaign/event calls
fall back to the owning account's preferred_language (see
call_orchestrator.create_campaign_call / tasks/calling.py's send_campaign),
since a campaign message has no language field of its own today.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.rest import Client
from twilio.twiml.voice_response import Gather, Say, VoiceResponse

from app.core.config import settings
from app.models import PlatformConfig
from app.models.domain import VoiceTier

VOICE_CONFIG_KEY = "language_voice_map"

# Default/fallback locale -> (standard_voice, neural_voice) mapping — used
# only when no admin-managed override exists yet in PlatformConfig (see
# get_voice_map below). Once an admin edits the map via the internal panel
# (app/api/internal/voice_config.py), THAT becomes the live source of
# truth; this dict is never touched again after the first override is
# saved. Twilio supports ~60 locales — unlisted locales fall back to "en"
# whether sourced from here or from the admin-managed map.
#
# "vi" was previously here mapped to Polly.Hiujin — which is actually a
# CANTONESE voice, not Vietnamese. Amazon Polly has no real Vietnamese
# voice at all (confirmed against Polly's official supported-language
# list). Removing this fake entry from the SOURCE default, not just the
# live database row — the earlier fix only cleared the bad row via SQL,
# which meant a fresh DB wipe self-seeded this same buggy default right
# back (see /admin/voice-config's TWILIO_SUPPORTED_LANGUAGES validation,
# which now also blocks "vi" from ever being re-added as supported).
DEFAULT_VOICE_MAP: dict[str, list[str]] = {
    "en": ["Polly.Joanna", "Polly.Joanna-Neural"],
    "en-GB": ["Polly.Amy", "Polly.Amy-Neural"],
    "es": ["Polly.Lupe", "Polly.Lupe-Neural"],
    "fr": ["Polly.Celine", "Polly.Lea-Neural"],
    "de": ["Polly.Marlene", "Polly.Vicki-Neural"],
    "ja": ["Polly.Mizuki", "Polly.Kazuha-Neural"],
}

# The keypress menu spoken right after the opening message — previously
# hardcoded English regardless of the call's actual language, meaning a
# Japanese-voice call would correctly speak the opening line in Japanese,
# then immediately speak this menu in English with a Japanese-accented
# voice. English is the required fallback for any language not yet listed
# here, same pattern as confirmation_templates.py.
_MENU_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "base": "Press 1 to confirm. Press 2 to cancel.",
        "voice_message": "Press 3 to leave a message.",
        "report": "Press 9 if this call seems suspicious or unexpected.",
    },
    "ja": {
        "base": "確認の場合は1を、キャンセルの場合は2を押してください。",
        "voice_message": "伝言を残す場合は3を押してください。",
        "report": "不審な着信だと思われる場合は9を押してください。",
    },
}


def _menu_text(language: str, voice_messaging_enabled: bool) -> str:
    strings = _MENU_TEXT.get(language, _MENU_TEXT["en"])
    parts = [strings["base"]]
    if voice_messaging_enabled:
        parts.append(strings["voice_message"])
    parts.append(strings["report"])
    return " ".join(parts)


_RECORDING_SAVED_TEXT: dict[str, str] = {
    "en": "Your message has been recorded. Goodbye.",
    "ja": "メッセージを承りました。失礼いたします。",
}


def recording_saved_text(language: str) -> str:
    return _RECORDING_SAVED_TEXT.get(language, _RECORDING_SAVED_TEXT["en"])


def _split_and_tag(text: str, proper_nouns: list[dict]) -> list[tuple[str, str | None]]:
    """Splits `text` into (segment, lang_or_None) pairs — proper-noun spans
    get their own locale, everything else is None (meaning: use the base
    voice/language). Matches left-to-right by literal substring; a proper
    noun not actually found in the text (e.g. stale data from an edited
    detail) is simply skipped rather than raising."""
    segments: list[tuple[str, str | None]] = []
    remaining = text
    while remaining:
        earliest_idx, earliest_pn = None, None
        for pn in proper_nouns:
            idx = remaining.find(pn["text"])
            if idx != -1 and (earliest_idx is None or idx < earliest_idx):
                earliest_idx, earliest_pn = idx, pn
        if earliest_pn is None:
            segments.append((remaining, None))
            break
        if earliest_idx > 0:
            segments.append((remaining[:earliest_idx], None))
        segments.append((earliest_pn["text"], earliest_pn["language"]))
        remaining = remaining[earliest_idx + len(earliest_pn["text"]):]
    return segments


def say_with_proper_nouns(parent, text: str, voice: str, language: str, proper_nouns: list[dict] | None):
    """Speaks `text` using `voice`/`language` throughout, except any
    identified proper-noun spans (names/places/brands), which are wrapped in
    an SSML <lang> tag so they keep their own correct pronunciation instead
    of being forced through the base voice's phonetics. Falls back to a
    plain <Say> when there are no proper nouns to tag, or none are actually
    found in this specific text."""
    if not proper_nouns:
        return parent.say(text, voice=voice, language=language)
    segments = _split_and_tag(text, proper_nouns)
    if all(lang is None for _, lang in segments):
        return parent.say(text, voice=voice, language=language)
    say = Say(voice=voice, language=language)
    for segment_text, segment_lang in segments:
        if not segment_text:
            continue
        if segment_lang:
            say.lang(segment_text, xml_lang=segment_lang)
        else:
            say.append(segment_text)
    return parent.nest(say)


def get_client() -> Client:
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


async def get_voice_map(db: AsyncSession) -> dict[str, list[str]]:
    """The live language -> [standard_voice, neural_voice] map — reads
    the admin-managed override from PlatformConfig, self-seeding it with
    DEFAULT_VOICE_MAP the first time this is ever called if no row exists
    yet. This means the database always holds the real, live config after
    the very first request — not a "still hardcoded until an admin
    happens to save something" gap. From that point on, admin edits via
    PUT /voice-config are what's actually live; this function never falls
    back to the Python constant again once a row exists."""
    row = await db.get(PlatformConfig, VOICE_CONFIG_KEY)
    if row and row.value:
        return row.value
    if row is None:
        db.add(PlatformConfig(key=VOICE_CONFIG_KEY, value=DEFAULT_VOICE_MAP))
        await db.commit()
    return DEFAULT_VOICE_MAP


async def voice_for(db: AsyncSession, language: str, tier: VoiceTier) -> str:
    voice_map = await get_voice_map(db)
    std, neural = voice_map.get(language, voice_map.get("en", DEFAULT_VOICE_MAP["en"]))
    return neural if tier == VoiceTier.neural else std


async def build_reminder_twiml(
    db: AsyncSession,
    message: str,
    language: str,
    tier: VoiceTier,
    call_id: str,
    voice_messaging_enabled: bool,
    proper_nouns: list[dict] | None = None,
) -> str:
    """Confirm(1) / decline(2) / voice message(3, add-on only) / report(9).

    Press 9 ("this call seems suspicious or unexpected") is always present —
    it is a recipient-facing protection and feeds the risk score.

    HARD BLOCK: this is the only keypress surface in the system; it never
    collects digits beyond a single menu keypress, so card numbers / SSNs /
    PINs are structurally impossible to capture here.
    """
    voice = await voice_for(db, language, tier)
    resp = VoiceResponse()
    gather = Gather(
        num_digits=1,
        action=f"{settings.twilio_webhook_base_url}/api/public/v1/webhooks/twilio/gather/{call_id}",
        method="POST",
        timeout=8,
    )
    say_with_proper_nouns(gather, message, voice, language, proper_nouns)
    menu = _menu_text(language, voice_messaging_enabled)
    gather.say(menu, voice=voice, language=language)
    resp.append(gather)
    resp.say("We did not receive a response. Goodbye.", voice=voice, language=language)
    return str(resp)


async def build_record_twiml(db: AsyncSession, call_id: str, language: str, tier: VoiceTier) -> str:
    voice = await voice_for(db, language, tier)
    resp = VoiceResponse()
    resp.say("Please leave your message after the tone.", voice=voice, language=language)
    resp.record(
        max_length=120,
        action=f"{settings.twilio_webhook_base_url}/api/public/v1/webhooks/twilio/recording/{call_id}",
        method="POST",
    )
    return str(resp)


def place_call(to_number: str, from_number: str, call_id: str) -> str:
    """Dial out; Twilio fetches TwiML from our answer webhook. Returns CallSid.

    We rely on Twilio's default queued dialing / rate limits — calls beyond the
    CPS limit queue on Twilio's side rather than fail (no custom parallel-dial
    infrastructure in Phase 1).
    """
    client = get_client()
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=f"{settings.twilio_webhook_base_url}/api/public/v1/webhooks/twilio/answer/{call_id}",
        status_callback=f"{settings.twilio_webhook_base_url}/api/public/v1/webhooks/twilio/status/{call_id}",
        status_callback_event=["completed", "no-answer", "busy", "failed"],
        method="POST",
    )
    return call.sid


def purchase_number(country: str) -> tuple[str, str]:
    """Buy a local (fallback: mobile) number for `country`. Returns (number, sid)."""
    client = get_client()
    try:
        candidates = client.available_phone_numbers(country).local.list(limit=1)
    except Exception:
        candidates = []
    if not candidates:
        try:
            candidates = client.available_phone_numbers(country).mobile.list(limit=1)
        except Exception:
            candidates = []
    if not candidates:
        raise RuntimeError(f"No Twilio numbers available for country {country}")
    bought = client.incoming_phone_numbers.create(phone_number=candidates[0].phone_number)
    return bought.phone_number, bought.sid


def send_sms(to_number: str, from_number: str, body: str) -> None:
    get_client().messages.create(to=to_number, from_=from_number, body=body)