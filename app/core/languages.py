"""Canonical list of selectable call languages — must stay in sync with the
frontend's src/lib/languages.ts. Not every one of these has a real mapped
TTS voice yet (see twilio_service.VOICE_MAP) or translated confirmation
templates (see confirmation_templates.py) — those are separate, narrower
lists that fall back to English where a specific language isn't covered
yet. This list is just "a value the language field/CSV column is allowed
to contain", matching what the frontend actually offers as an option.
"""
VALID_LANGUAGES: set[str] = {
    "en", "en-GB", "en-AU", "en-IN",
    "es", "es-MX",
    "fr", "fr-CA",
    "de", "it", "pt", "pt-BR", "nl", "ru", "pl", "tr", "ar",
    "hi", "bn", "ta", "te", "mr", "gu", "pa", "kn", "ml",
    "zh", "zh-HK", "ja", "ko", "vi", "th", "id", "ms", "fil",
    "sv", "nb", "da", "fi",
}