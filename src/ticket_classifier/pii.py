"""Regex-based PII redaction, run before any ticket text leaves the process.

Covers email addresses, phone numbers, payment card numbers, IBANs, US Social Security
numbers and IP addresses. Names and postal addresses need an NER model and are out of
scope; see the README's limitations section.
"""

import ipaddress
import re
from dataclasses import dataclass
from typing import Final

from ticket_classifier.models import PiiType

PLACEHOLDERS: Final[dict[PiiType, str]] = {
    PiiType.EMAIL: "[EMAIL]",
    PiiType.PHONE: "[PHONE]",
    PiiType.CARD: "[CARD]",
    PiiType.IBAN: "[IBAN]",
    PiiType.SSN: "[SSN]",
    PiiType.IP_ADDRESS: "[IP]",
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
# Country code, check digits, then 11-30 alphanumerics, optionally grouped in fours.
_IBAN_RE: Final = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_IBAN_LENGTHS: Final = range(15, 35)
# Dashes are required: a bare 9-digit number is far more likely an order or tracking number.
_SSN_RE: Final = re.compile(r"(?<![\d-])(\d{3})-(\d{2})-(\d{4})(?![\d-])")
_IPV4_RE: Final = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d|\.\d)")
_IPV6_RE: Final = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")


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


def _is_iban(candidate: str) -> bool:
    # ISO 13616 check: move the first four characters to the end, map letters to 10..35,
    # and the resulting number mod 97 must be 1.
    compact = candidate.replace(" ", "")
    if len(compact) not in _IBAN_LENGTHS:
        return False
    rearranged = compact[4:] + compact[:4]
    return int("".join(str(int(char, 36)) for char in rearranged)) % 97 == 1


def _is_ssn(area: str, group: str, serial: str) -> bool:
    # The SSA never issues area 000, 666 or 900-999, group 00 or serial 0000.
    return area not in ("000", "666") and area[0] != "9" and group != "00" and serial != "0000"


def _is_ip(candidate: str) -> bool:
    # A bare "::" is technically a valid IPv6 address but is just punctuation in prose.
    if not any(char.isalnum() for char in candidate):
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def redact_pii(text: str) -> RedactionResult:
    spans: list[tuple[int, int, PiiType]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < taken_end and taken_start < end for taken_start, taken_end, _ in spans)

    for match in _EMAIL_RE.finditer(text):
        spans.append((match.start(), match.end(), PiiType.EMAIL))

    # IBANs and IP addresses go before cards and phones: their digit runs would otherwise
    # be mistaken for (or partly claimed by) those looser patterns.
    for match in _IBAN_RE.finditer(text):
        if not overlaps(*match.span()) and _is_iban(match.group()):
            spans.append((match.start(), match.end(), PiiType.IBAN))

    for pattern in (_IPV4_RE, _IPV6_RE):
        for match in pattern.finditer(text):
            if not overlaps(*match.span()) and _is_ip(match.group()):
                spans.append((match.start(), match.end(), PiiType.IP_ADDRESS))

    for match in _CARD_RE.finditer(text):
        # Luhn filters out long order and tracking numbers that are not card numbers.
        if not overlaps(*match.span()) and _passes_luhn(_digits(match.group())):
            spans.append((match.start(), match.end(), PiiType.CARD))

    for match in _SSN_RE.finditer(text):
        if not overlaps(*match.span()) and _is_ssn(*match.groups()):
            spans.append((match.start(), match.end(), PiiType.SSN))

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
