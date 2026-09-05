"""AI-assisted rephrasing for CSV bulk upload's "reason" field.

Narrower and cheaper than the single-appointment sentence_check.py flow (no
dual-language tabs, no interactive per-row resolution — CSV rows go through
validation without any per-row UI), but still genuinely AI-powered, because
the actual problem here isn't spelling/grammar — it's that arbitrary
user-typed text gets inserted into a FIXED TEMPLATE SLOT, and naive text can
break that grammatically. Example: template "...for {reason}, at {time}."
+ user types "for a table" -> "...for for a table, at..." — a rule-based
grammar checker (LanguageTool) cannot see or fix this, since it only checks
a standalone sentence, not "does this fragment fit once inserted into
someone else's sentence." That requires real language understanding.

Always produces a best-effort naturalized version in the row's own declared
language — never rejects a row over phrasing, unlike the single-form flow's
language-mismatch tabs (which don't make sense for a non-interactive bulk
row). Falls back to the original text, no proper nouns, on any failure —
never blocks a CSV upload.
"""
import json

from app.core.config import settings

_LANGUAGE_NAMES = {
    "en": "English", "en-GB": "English", "en-AU": "English", "en-IN": "English",
    "ja": "Japanese", "vi": "Vietnamese", "es": "Spanish", "es-MX": "Spanish",
    "fr": "French", "fr-CA": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "pt-BR": "Portuguese", "zh": "Chinese", "ko": "Korean",
    "th": "Thai", "id": "Indonesian",
}


def rephrase_reason(text: str, language: str, hint: str) -> dict:
    """`hint` is the raw template string with the {subject_detail} slot
    still visible (see confirmation_templates.template_hint), so the AI can
    see exactly how the text gets inserted. Returns
    {"corrected": str, "proper_nouns": [{"text": str, "language": str}]}."""
    if not text.strip():
        return {"corrected": text, "proper_nouns": []}
    api_key = settings.openai_api_key
    if not api_key:
        return {"corrected": text, "proper_nouns": []}
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        lang_name = _LANGUAGE_NAMES.get(language, language)
        prompt = (
            "This phrase will be inserted into a fixed sentence template, spoken aloud "
            f"by a text-to-speech voice in {lang_name} ({language}):\n"
            f'Template: "{hint}" (the phrase replaces {{subject_detail}})\n'
            f'Phrase as typed: "{text}"\n\n'
            f"Rewrite the phrase so it reads naturally and grammatically correct once "
            f"inserted into that exact template slot, in {lang_name} — in particular, "
            f"remove any word the user typed that duplicates wording the template "
            f"already provides right before the slot (e.g. don't repeat a connecting "
            f"word like 'for' or 'regarding' if the template already has it there). "
            f"Keep the same meaning. Preserve any proper nouns (person names, business/"
            f"brand names, place names) EXACTLY as written, never translate them. If the "
            f"phrase already fits correctly as-is, return it unchanged.\n\n"
            "Also list every proper noun found in the phrase — the exact substring as "
            "it literally appears — with the BCP-47 locale it should be pronounced in "
            "natively, based on what that specific name/place actually is (not the "
            "sentence's language).\n\n"
            "Respond ONLY with JSON, no other text:\n"
            '{"corrected": "<rewritten phrase>", '
            '"proper_nouns": [{"text": "<substring>", "language": "<bcp-47 locale>"}]}'
        )
        completion = client.chat.completions.create(
            model="gpt-5-mini",
            max_completion_tokens=1000,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(completion.choices[0].message.content.strip())
        return {
            "corrected": result.get("corrected") or text,
            "proper_nouns": [
                pn for pn in result.get("proper_nouns", [])
                if pn.get("text") and pn.get("language")
            ],
        }
    except Exception:
        return {"corrected": text, "proper_nouns": []}