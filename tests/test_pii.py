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
