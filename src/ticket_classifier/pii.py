"""Regex-based PII redaction, run before any ticket text leaves the process.

Covers email addresses, phone numbers and payment card numbers. Names and postal
addresses need an NER model and are out of scope; see the README's limitations section.
"""

import re
from dataclasses import dataclass
from typing import Final

from ticket_classifier.models import PiiType

PLACEHOLDERS: Final[dict[PiiType, str]] = {
    PiiType.EMAIL: "[EMAIL]",
    PiiType.PHONE: "[PHONE]",
    PiiType.CARD: "[CARD]",
}

_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# 13-19 digits, optionally grouped with single spaces or hyphens.
_CARD_RE: Final = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
# Loose candidate; the digit-count check below decides. A leading '#' marks an order or
# ticket reference, so it is excluded.
_PHONE_RE: Final = re.compile(
    r"(?<![\w#+])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d[\d .-]{6,}\d(?!\w)"
)
_PHONE_DIGITS: Final = range(10, 16)  # E.164 allows at most 15 digits


@dataclass(frozen=True, slots=True)
class RedactionResult:
    text: str
    found: tuple[PiiType, ...]

    @property
    def pii_detected(self) -> bool:
        return bool(self.found)


def _passes_luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def redact_pii(text: str) -> RedactionResult:
    spans: list[tuple[int, int, PiiType]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < taken_end and taken_start < end for taken_start, taken_end, _ in spans)

    for match in _EMAIL_RE.finditer(text):
        spans.append((match.start(), match.end(), PiiType.EMAIL))

    for match in _CARD_RE.finditer(text):
        # Luhn filters out long order and tracking numbers that are not card numbers.
        if not overlaps(*match.span()) and _passes_luhn(_digits(match.group())):
            spans.append((match.start(), match.end(), PiiType.CARD))

    for match in _PHONE_RE.finditer(text):
        candidate = match.group().strip()
        start = match.start() + match.group().index(candidate)
        end = start + len(candidate)
        if len(_digits(candidate)) in _PHONE_DIGITS and not overlaps(start, end):
            spans.append((start, end, PiiType.PHONE))

    if not spans:
        return RedactionResult(text=text, found=())

    redacted = text
    for start, end, pii_type in sorted(spans, reverse=True):
        redacted = redacted[:start] + PLACEHOLDERS[pii_type] + redacted[end:]

    found = tuple(pii_type for pii_type in PiiType if any(s[2] is pii_type for s in spans))
    return RedactionResult(text=redacted, found=found)
