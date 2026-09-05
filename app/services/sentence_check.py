"""AI-assisted sentence check for appointment subject_detail text.

Simplified design: language mismatch is a hard block, not a choice — if the
subject_detail's structural language doesn't match the selected call
language, creation is refused outright (the frontend shows an error, no
dialog). There are no tabs and no "pick original vs corrected" choice
anymore; on a match, only the single AI-generated sentence is shown as a
confirm step before creating.

Still does, in one AI call:
  1. Structural-language-match judgment (proper nouns ignored — a foreign
     name/place embedded in an otherwise-matching phrase doesn't count).
  2. Naturalized/grammar-corrected phrasing, only when matched.
  3. Proper-noun identification (client name, business name, phrase) for
     SSML <lang> tagging at actual call time.

Falls back to a "trust the input, treat as matched" result if no API key is
configured or the call fails — this must never block appointment creation
when AI itself is unavailable, only when a genuine mismatch is detected.
"""
import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import ConfirmationType
from app.schemas.common import ProperNoun, SentenceCheckOut, SentenceCheckRequest
from app.services import confirmation_templates as templates

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {
    "en": "English", "ja": "Japanese", "vi": "Vietnamese", "es": "Spanish",
    "fr": "French", "de": "German", "en-GB": "English",
}


async def _render(db: AsyncSession, conf_type: ConfirmationType, client_name: str, business: str,
                   time_str: str, detail: str | None, language: str) -> str:
    return await templates.render_opening(db, conf_type, client_name, business, time_str, detail, language=language)


async def _fallback(
    db: AsyncSession, req: SentenceCheckRequest, business: str, time_str: str, conf_type: ConfirmationType,
) -> SentenceCheckOut:
    """No AI available — trust the input as-is, treated as matched, no
    proper-noun tagging (same behavior as before this feature existed)."""
    rendered = await _render(db, conf_type, req.client_name, business, time_str, req.subject_detail, req.language)
    return SentenceCheckOut(
        language_match=True, generated=rendered, detail=req.subject_detail or "", proper_nouns=[],
    )


async def check_sentence(
    db: AsyncSession, req: SentenceCheckRequest, business: str, time_str: str,
) -> SentenceCheckOut:
    conf_type = ConfirmationType(req.confirmation_type)
    detail = (req.subject_detail or "").strip()

    api_key = settings.openai_api_key
    if not api_key:
        logger.warning("sentence_check: OPENAI_API_KEY not configured — falling back, no AI check performed")
        return await _fallback(db, req, business, time_str, conf_type)
    # An empty `detail` does NOT skip the AI call — the client's name is
    # always present and can always need proper-noun tagging, even with no
    # subject_detail text to language-check.

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        selected_name = _LANGUAGE_NAMES.get(req.language, req.language)
        detail_section = (
            f'Phrase: "{detail}"\n'
            if detail else
            "Phrase: (none — this call has no subject_detail text; only check the "
            "client name and business name for proper nouns below.)\n"
        )
        prompt = (
            "A user is writing a short phrase that will be inserted into a fixed sentence "
            f"template, spoken aloud by a text-to-speech voice set to {selected_name} "
            f"({req.language}).\n\n"
            f"{detail_section}"
            f'Client name (also spoken aloud, same rules apply): "{req.client_name}"\n'
            f'Business name (also spoken aloud, same rules apply): "{business}"\n\n'
            "1. Judge the STRUCTURAL language of the phrase — the grammar/sentence "
            "construction — while IGNORING proper nouns (person names, business/brand "
            "names, place names), since a foreign proper noun embedded in an otherwise-"
            f"{selected_name} phrase does not change the phrase's actual language. "
            "Does the structural language match the target language above? "
            "(If there is no phrase, this is trivially true.)\n"
            "2. ONLY IF it matches: produce a naturally-phrased, grammatically correct "
            "version of the phrase in the target language — same meaning, same proper "
            "nouns preserved EXACTLY and never translated, only grammar/naturalness "
            "improved. If it's already correct, return it unchanged. If there is no "
            "phrase, return an empty string. If it does NOT match, leave this empty — "
            "no need to generate anything.\n"
            "3. List every proper noun (person name, business/brand name, place name) "
            "found in the phrase, the client name, OR the business name — the exact "
            "substring as it literally appears — along with the BCP-47 locale it should "
            "be pronounced in natively (e.g. \"ja-JP\", \"en-US\", \"vi-VN\"), based on what "
            "that specific name/place actually is, not the sentence's language. Do not "
            "include common nouns, only proper nouns. If there are none, return an empty "
            "list.\n\n"
            "Respond ONLY with JSON, no other text:\n"
            "{\n"
            '  "language_match": true|false,\n'
            '  "detected_language": "<ISO 639-1 code of the phrase\'s actual structural '
            'language, or null if it matches the target or there is no phrase>",\n'
            '  "generated": "<naturalized phrase, ONLY if language_match is true, else '
            'empty string>",\n'
            '  "proper_nouns": [{"text": "<exact substring>", "language": "<bcp-47 locale>"}]\n'
            "}"
        )
        completion = client.chat.completions.create(
            model="gpt-5-mini",
            max_completion_tokens=1500,
            reasoning_effort="low",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(completion.choices[0].message.content.strip())
        language_match = bool(result.get("language_match", True))
        logger.info(
            "sentence_check: AI call succeeded — language_match=%s detected_language=%s",
            language_match, result.get("detected_language"),
        )
        proper_nouns = [
            ProperNoun(text=pn["text"], language=pn["language"])
            for pn in result.get("proper_nouns", [])
            if pn.get("text") and pn.get("language")
        ]

        if not language_match:
            return SentenceCheckOut(
                language_match=False,
                detected_language=result.get("detected_language"),
                proper_nouns=proper_nouns,  # still useful context even though creation is blocked
            )

        naturalized_detail = result.get("generated") or detail
        generated_sentence = await _render(
            db, conf_type, req.client_name, business, time_str, naturalized_detail, req.language
        )
        return SentenceCheckOut(
            language_match=True, generated=generated_sentence, detail=naturalized_detail, proper_nouns=proper_nouns,
        )
    except Exception:
        # AI failed/unavailable — never block appointment creation over this;
        # fall back to trusting the input exactly as typed. Logged with full
        # traceback so a silent failure is actually diagnosable, instead of
        # looking identical to a genuine "AI said this matches" result.
        logger.exception("sentence_check: AI call failed, falling back to trusting the input as-is")
        return await _fallback(db, req, business, time_str, conf_type)