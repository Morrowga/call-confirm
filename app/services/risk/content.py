"""Trust & safety — layers 1 & 2 (content) plus structural hard blocks.

Layer 1: keyword validation flags both fear-based and excitement-based scam
language. Layer 2 scores how far a custom message deviates from the account's
expected controlled template shape.

Hard blocks are code-level rejections, not score contributions: templates may
never request payment details, never use prize/winner framing, and no call flow
may capture sensitive data.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

FEAR_KEYWORDS = [
    "legal action", "final notice", "lawsuit", "arrest", "warrant", "police",
    "penalty", "seized", "frozen account", "immediately or", "last warning",
    "debt collector", "court",
]
EXCITEMENT_KEYWORDS = [
    "you've won", "you have won", "winner", "claim now", "selected", "prize",
    "reward", "congratulations you", "free gift", "cash prize", "lucky",
    "act now", "limited time offer",
]
# Hard-block patterns: payment/sensitive-data requests are never allowed.
HARD_BLOCK_PATTERNS = [
    r"card\s*number", r"credit\s*card", r"debit\s*card", r"\bcvv\b", r"\bcvc\b",
    r"bank\s*(account|details|transfer)", r"routing\s*number", r"\biban\b",
    r"social\s*security", r"\bssn\b", r"\bpin\b(?!\w)", r"password",
    r"enter\s+your\s+(card|account|pin|social)", r"payment\s+(details|information)",
    r"wire\s+(money|transfer|funds)", r"gift\s*card", r"bitcoin|crypto\s*wallet",
]
PRIZE_FRAME_PATTERNS = [r"\bprize\b", r"\bwinner\b", r"\breward\b", r"\bselected\b"]

# The controlled template shapes messages are expected to resemble.
EXPECTED_TEMPLATES = {
    "appointment": (
        "Hello {name}, this is a reminder of your appointment with {business} "
        "on {date} at {time}."
    ),
    "event": (
        "Hello {name}, {artist} is planning an event on {date}. "
        "We're checking interest before tickets go on sale."
    ),
}


@dataclass
class ContentCheck:
    hard_blocked: bool = False
    hard_block_reasons: list[str] = field(default_factory=list)
    keyword_score: int = 0            # layer 1 contribution (0-40)
    keyword_hits: list[str] = field(default_factory=list)
    deviation_score: int = 0          # layer 2 contribution (0-30)
    uses_reward_framing: bool = False


def check_hard_blocks(message: str) -> tuple[bool, list[str]]:
    text = message.lower()
    reasons = []
    for pattern in HARD_BLOCK_PATTERNS:
        if re.search(pattern, text):
            reasons.append(f"requests payment/sensitive data: /{pattern}/")
    for pattern in PRIZE_FRAME_PATTERNS:
        if re.search(pattern, text):
            reasons.append(f"prize/winner/reward/selected framing: /{pattern}/")
    return bool(reasons), reasons


def keyword_scan(message: str) -> tuple[int, list[str]]:
    text = message.lower()
    hits = [kw for kw in FEAR_KEYWORDS + EXCITEMENT_KEYWORDS if kw in text]
    return min(len(hits) * 12, 40), hits


def template_deviation(message: str, kind: str) -> int:
    """0 (matches template shape) .. 30 (completely off-template)."""
    template = EXPECTED_TEMPLATES.get(kind, EXPECTED_TEMPLATES["appointment"])
    skeleton = re.sub(r"\{[^}]+\}", "", template).lower()
    normalized = re.sub(r"\s+", " ", message.lower())
    similarity = SequenceMatcher(None, skeleton, normalized).ratio()
    return int(round((1 - similarity) * 30))


def evaluate_content(message: str, kind: str) -> ContentCheck:
    result = ContentCheck()
    result.hard_blocked, result.hard_block_reasons = check_hard_blocks(message)
    result.keyword_score, result.keyword_hits = keyword_scan(message)
    result.deviation_score = template_deviation(message, kind)
    result.uses_reward_framing = any(
        re.search(p, message.lower()) for p in PRIZE_FRAME_PATTERNS
    )
    return result
