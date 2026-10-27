"""Guards against silent breaking changes to released prompts and to the HTTP API.

The API snapshots are backward-compatibility checks, not exact matches: adding an optional
field passes, but removing or changing an existing field or enum value fails. A deliberate
breaking change belongs in a new API version with a new snapshot, not an edited one.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import BaseModel

from ticket_classifier.api import ClassifyRequest
from ticket_classifier.models import TicketResult
from ticket_classifier.prompts import PROMPTS

# Released prompts are frozen: their eval results must stay comparable. To change a prompt,
# add a new version to PROMPTS and its hash here.
RELEASED_PROMPT_SHA256: Final = {
    "v1": "ee469c05511ef8d6997af3875f67677de3cd0caa58ace2745ae4e47f67496be9",
}

CONTRACTS: Final = Path(__file__).parent / "contracts"


@pytest.mark.parametrize("version", sorted(RELEASED_PROMPT_SHA256))
def test_released_prompts_are_unchanged(version: str) -> None:
    digest = hashlib.sha256(PROMPTS[version].system_prompt.encode()).hexdigest()

    assert digest == RELEASED_PROMPT_SHA256[version], (
        f"Prompt {version} was edited. Add a new version instead."
    )


def test_every_prompt_version_is_registered_as_released() -> None:
    assert set(PROMPTS) == set(RELEASED_PROMPT_SHA256)


def _load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((CONTRACTS / f"{name}.json").read_text())
    return loaded


def _assert_backward_compatible(snapshot: dict[str, Any], model: type[BaseModel]) -> None:
    current = model.model_json_schema()
    for field_name, field_schema in snapshot["properties"].items():
        assert field_name in current["properties"], f"field {field_name!r} was removed"
        assert current["properties"][field_name] == field_schema, f"field {field_name!r} changed"
    for name, definition in snapshot.get("$defs", {}).items():
        assert current.get("$defs", {}).get(name) == definition, f"type {name!r} changed"
    assert set(snapshot.get("required", [])) <= set(current.get("required", [])), (
        "a field that callers rely on is no longer required"
    )


def test_ticket_result_is_backward_compatible() -> None:
    _assert_backward_compatible(_load("ticket_result_v1"), TicketResult)


def test_classify_request_is_backward_compatible() -> None:
    snapshot = _load("classify_request_v1")
    current = ClassifyRequest.model_json_schema()

    _assert_backward_compatible(snapshot, ClassifyRequest)
    # A new request field must be optional, or existing callers' requests would be rejected.
    assert set(current.get("required", [])) == set(snapshot.get("required", []))


def test_contract_check_catches_a_removed_field() -> None:
    snapshot = _load("ticket_result_v1")
    snapshot["properties"]["no_longer_there"] = {"type": "string"}

    with pytest.raises(AssertionError, match="no_longer_there"):
        _assert_backward_compatible(snapshot, TicketResult)
