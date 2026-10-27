"""Cheap pre-filter for blatant prompt-injection attempts.

This only catches the obvious cases so they never cost a Claude call. The real defence
is the prompt design: the ticket is passed as delimited data, Claude reports
``injection_suspected`` for anything subtler, and the output schema leaves no room for
free-form actions.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

# Words that point at the assistant's own instructions ("previous", "the above"), as opposed
# to a customer talking about their own ("my previous instructions about delivery").
_QUALIFIER: Final = r"(?:above|previous|prior|earlier|preceding|initial|original|system)"
_TARGET: Final = (
    rf"(?:(?:all|any)\s+(?:of\s+)?(?:the\s+|your\s+)?(?:{_QUALIFIER}\s+)?"
    rf"|your\s+(?:{_QUALIFIER}\s+)?"
    rf"|(?:the\s+)?{_QUALIFIER}\s+)"
)

_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "instruction_override": re.compile(
        rf"\b(ignore|disregard|forget|override)\s+{_TARGET}"
        r"(instructions?|prompts?|rules|guidelines)\b"
    ),
    "prompt_extraction": re.compile(
        r"\b(reveal|show|print|repeat|output|leak)\b.{0,30}\b(system|hidden|initial)\s+(prompt|instructions?)\b"
    ),
    "role_override": re.compile(
        r"\b(you are now|from now on you are|pretend (to be|you are))"
        # "act as" only when addressed to the assistant, not "I want to act as the holder".
        r"|(?:^|[.!?:]\s*|\b(?:you|now|please)\s+(?:will\s+|must\s+|should\s+)?)act as (an?|the) "
    ),
    "delimiter_escape": re.compile(r"</?\s*(ticket|system|instructions?)\s*>"),
}


@dataclass(frozen=True, slots=True)
class GuardResult:
    flagged: bool
    pattern: str | None = None


def _normalise(text: str) -> str:
    # NFKC folds look-alike characters (e.g. full-width letters) used to dodge filters.
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", folded)


def check_injection(text: str) -> GuardResult:
    normalised = _normalise(text)
    for name, pattern in _PATTERNS.items():
        if pattern.search(normalised):
            return GuardResult(flagged=True, pattern=name)
    return GuardResult(flagged=False)
