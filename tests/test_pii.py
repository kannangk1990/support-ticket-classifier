import pytest

from ticket_classifier.models import PiiType
from ticket_classifier.pii import redact_pii


def test_redacts_email_phone_and_card() -> None:
    text = "Email jane@example.com or call 415-555-0132. Card 4111 1111 1111 1111."

    result = redact_pii(text)

    assert result.text == "Email [EMAIL] or call [PHONE]. Card [CARD]."
    assert result.found == (PiiType.EMAIL, PiiType.PHONE, PiiType.CARD)
    assert result.pii_detected


@pytest.mark.parametrize(
    "phone",
    ["(415) 555-0132", "+1 415 555 0132", "+44 20 7946 0958", "415.555.0132", "4155550132"],
)
def test_redacts_common_phone_formats(phone: str) -> None:
    result = redact_pii(f"Call me on {phone} please")

    assert result.text == "Call me on [PHONE] please"


@pytest.mark.parametrize(
    "card", ["4111111111111111", "4111-1111-1111-1111", "5500 0000 0000 0004", "378282246310005"]
)
def test_redacts_luhn_valid_cards(card: str) -> None:
    assert redact_pii(f"card: {card}").text == "card: [CARD]"


@pytest.mark.parametrize(
    "text",
    [
        "Order #88213 has not arrived",
        "Tracking number 1234567812345678 shows no movement",  # 16 digits, fails Luhn
        "Order #4155550132 is late",  # '#' marks a reference, not a phone number
        "It arrived on 2026-10-26 at 14:05",
        "I paid 1,299.00 for it",
    ],
)
def test_leaves_non_pii_numbers_alone(text: str) -> None:
    result = redact_pii(text)

    assert result.text == text
    assert not result.pii_detected


def test_reports_each_type_once_in_stable_order() -> None:
    result = redact_pii("a@b.io then c@d.io then 415-555-0132")

    assert result.found == (PiiType.EMAIL, PiiType.PHONE)
    assert result.text == "[EMAIL] then [EMAIL] then [PHONE]"


@pytest.mark.parametrize(
    "iban", ["DE89 3704 0044 0532 0130 00", "GB82WEST12345698765432", "FR1420041010050500013M02606"]
)
def test_redacts_checksum_valid_ibans(iban: str) -> None:
    result = redact_pii(f"Refund to {iban} please")

    assert result.text == "Refund to [IBAN] please"
    assert result.found == (PiiType.IBAN,)


@pytest.mark.parametrize("ip", ["192.168.1.10", "8.8.8.8", "2001:db8::8a2e:370:7334"])
def test_redacts_ip_addresses(ip: str) -> None:
    result = redact_pii(f"Login from {ip}.")

    assert result.text == "Login from [IP]."
    assert result.found == (PiiType.IP_ADDRESS,)


def test_redacts_valid_us_ssn() -> None:
    result = redact_pii("My SSN is 123-45-6789 for the tax form")

    assert result.text == "My SSN is [SSN] for the tax form"
    assert result.found == (PiiType.SSN,)


@pytest.mark.parametrize(
    "text",
    [
        "Order DE12345678901234 is late",  # IBAN-shaped but fails the mod-97 check
        "Firmware 1.2.3 crashes",
        "Address 999.1.1.1 is not real",  # not a valid IPv4 address
        "The meeting is at 10:30:00 today",
        "Use the :: separator",
        "SSN 000-12-3456 is never issued",
        "Reference 123456789 without dashes",
    ],
)
def test_leaves_lookalike_numbers_alone(text: str) -> None:
    result = redact_pii(text)

    assert result.text == text
    assert not result.pii_detected


def test_new_pii_types_mix_with_existing_ones() -> None:
    text = "jane@example.com, IBAN GB82WEST12345698765432, IP 10.0.0.7, SSN 123-45-6789"

    result = redact_pii(text)

    assert result.text == "[EMAIL], IBAN [IBAN], IP [IP], SSN [SSN]"
    assert result.found == (PiiType.EMAIL, PiiType.IBAN, PiiType.SSN, PiiType.IP_ADDRESS)
