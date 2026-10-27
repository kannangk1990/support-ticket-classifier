import difflib

from ticket_classifier.config import Settings
from ticket_classifier.prompts import get_prompt


def test_v2_changes_only_the_high_priority_rule() -> None:
    v1 = get_prompt("v1").system_prompt.splitlines()
    v2 = get_prompt("v2").system_prompt.splitlines()

    removed = [line for line in difflib.ndiff(v1, v2) if line.startswith("- ")]
    added = [line for line in difflib.ndiff(v1, v2) if line.startswith("+ ")]

    assert removed == [
        "- - high: money charged with nothing received, or the customer is blocked from using "
        "the service."
    ]
    assert added == [
        "+ - high: money charged with nothing received, a parcel that is 3 or more days late "
        "or whose",
        "+   tracking has not changed for 3 or more days, or the customer is blocked from using "
        "the",
        "+   service.",
    ]


def test_v2_makes_three_day_delivery_delays_high() -> None:
    prompt = " ".join(get_prompt("v2").system_prompt.split())

    assert "a parcel that is 3 or more days late" in prompt
    assert "tracking has not changed for 3 or more days" in prompt


def test_default_prompt_is_v2() -> None:
    assert Settings(_env_file=None).prompt_version == "v2"
