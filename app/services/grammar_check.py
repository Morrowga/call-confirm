"""Grammar correction AND language detection for CSV bulk upload's "reason"
field, via a self-hosted LanguageTool server — genuinely free regardless of
volume, unlike the AI sentence-check flow used for the single-appointment
form (which does more: naturalization, proper-noun identification). CSV
rows get grammar/spelling fixes plus a language-mismatch check — no
translation, no proper-noun handling, since running the full AI flow across
up to 100 rows per upload would mean up to 100 real paid API calls.
"""
import httpx

from app.core.config import settings

# LanguageTool's accepted language codes differ slightly from ours in a few
# cases. Falls back to "auto" (LanguageTool's own detection) for anything
# not explicitly mapped.
_LT_LANGUAGE_MAP = {
    "en": "en-US",
    "en-GB": "en-GB",
    "ja": "ja-JP",
    "es": "es",
    "fr": "fr",
    "de": "de-DE",
}

# Below this confidence, a detected-language mismatch is NOT reported — the
# "reason" field is intentionally short (a label, not a sentence), and
# language detection is genuinely less reliable on short text. Silently
# trusting the user's declared language in that case is safer than
# rejecting a valid file over an unreliable guess.
_MISMATCH_CONFIDENCE_THRESHOLD = 0.55


def _language_base(code: str | None) -> str:
    """'en-US' -> 'en', 'ja-JP' -> 'ja' — compares at the base-language
    level, not the exact locale variant."""
    return (code or "").split("-")[0].lower()


def check_text(text: str, declared_language: str) -> dict:
    """Returns {"corrected": str, "language_mismatch": bool,
    "detected_language": str|None}. On any failure (LanguageTool
    unreachable, unsupported language, etc.), returns the text unchanged
    with language_mismatch=False — never blocks a CSV upload over a
    grammar-checking hiccup."""
    if not text.strip():
        return {"corrected": text, "language_mismatch": False, "detected_language": None}
    try:
        resp = httpx.post(
            f"{settings.languagetool_url}/v2/check",
            data={
                "text": text, "language": "auto",
                "preferredLanguages": _LT_LANGUAGE_MAP.get(declared_language, declared_language),
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        payload = resp.json()

        detected = payload.get("language", {}).get("detectedLanguage", {})
        detected_code = detected.get("code")
        confidence = detected.get("confidence") or 0.0
        mismatch = (
            confidence >= _MISMATCH_CONFIDENCE_THRESHOLD
            and _language_base(detected_code) != _language_base(declared_language)
        )

        matches = payload.get("matches", [])
        # Apply right-to-left so each edit's offset isn't shifted by an
        # earlier one changing the string's length.
        corrected = text
        for match in sorted(matches, key=lambda m: m["offset"], reverse=True):
            replacements = match.get("replacements", [])
            if not replacements:
                continue
            start = match["offset"]
            end = start + match["length"]
            corrected = corrected[:start] + replacements[0]["value"] + corrected[end:]

        return {"corrected": corrected, "language_mismatch": mismatch, "detected_language": detected_code}
    except Exception:
        return {"corrected": text, "language_mismatch": False, "detected_language": None}


def fix_grammar(text: str, language: str) -> str:
    """Grammar-only, no mismatch check — kept for callers that don't need
    detection."""
    return check_text(text, language)["corrected"]