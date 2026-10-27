import sqlite3
from pathlib import Path

import pytest

from tests.fakes import FakeLLM, response_with
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.llm import LLMUnavailableError
from ticket_classifier.models import ClassificationStatus, TicketResult
from ticket_classifier.storage import SCHEMA_VERSION, DuplicateExternalIdError, TicketStore

PII_TICKET = "Parcel late. Email jane@example.com, call 415-555-0132."


async def make_result(ok: bool = True, text: str = PII_TICKET) -> TicketResult:
    settings = Settings(max_attempts=1, _env_file=None)
    step = response_with() if ok else LLMUnavailableError("down")
    return await TicketClassifier(FakeLLM([step]), settings).classify(text)


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    return TicketStore(tmp_path / "nested" / "tickets.db")


async def test_round_trips_a_result(store: TicketStore) -> None:
    result = await make_result()

    store.save(result)

    assert store.get(result.ticket_id) == result
    assert store.get("missing") is None


async def test_looks_up_by_external_id(store: TicketStore) -> None:
    result = (await make_result()).model_copy(update={"external_id": "ZD-1001"})

    store.save(result)

    assert store.get_by_external_id("ZD-1001") == result
    assert store.get_by_external_id("ZD-9999") is None


async def test_saving_again_updates_the_row_and_keeps_created_at(
    store: TicketStore, tmp_path: Path
) -> None:
    fallback = await make_result(ok=False)
    store.save(fallback)
    classified = (await make_result()).model_copy(update={"ticket_id": fallback.ticket_id})

    store.save(classified)

    assert store.get(fallback.ticket_id) == classified
    with sqlite3.connect(tmp_path / "nested" / "tickets.db") as conn:
        created, updated = conn.execute(
            "SELECT created_at, updated_at FROM tickets WHERE ticket_id = ?",
            (fallback.ticket_id,),
        ).fetchone()
    assert created <= updated


async def test_rejects_a_second_ticket_with_the_same_external_id(store: TicketStore) -> None:
    store.save((await make_result()).model_copy(update={"external_id": "ZD-1"}))

    with pytest.raises(DuplicateExternalIdError):
        store.save((await make_result()).model_copy(update={"external_id": "ZD-1"}))


async def test_lists_only_fallbacks_oldest_first(store: TicketStore) -> None:
    first, second = await make_result(ok=False), await make_result(ok=False)
    store.save(first)
    store.save(await make_result())
    store.save(second)

    fallbacks = store.list_fallbacks()

    assert [r.ticket_id for r in fallbacks] == [first.ticket_id, second.ticket_id]
    assert all(r.status is ClassificationStatus.FALLBACK for r in fallbacks)
    assert [r.ticket_id for r in store.list_fallbacks(limit=1)] == [first.ticket_id]


async def test_never_stores_the_original_text(store: TicketStore, tmp_path: Path) -> None:
    store.save(await make_result())

    raw = (tmp_path / "nested" / "tickets.db").read_bytes()
    assert b"jane@example.com" not in raw
    assert b"415-555-0132" not in raw
    assert b"[EMAIL]" in raw  # the redacted text is what gets stored


def test_records_the_schema_version(store: TicketStore, tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "nested" / "tickets.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
