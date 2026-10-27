import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.fakes import FakeLLM, response_with
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.llm import LLMUnavailableError
from ticket_classifier.models import ClassificationStatus, TicketResult
from ticket_classifier.storage import (
    PURGED_TEXT,
    SCHEMA_VERSION,
    DuplicateExternalIdError,
    TicketStore,
)

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


NAME_DETAIL = "my neighbour Bob signed for it"


def age_row(path: Path, ticket_id: str, days: int) -> None:
    created = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE tickets SET created_at = ? WHERE ticket_id = ?", (created, ticket_id))


async def test_purge_erases_text_and_reasoning_but_keeps_the_classification(
    store: TicketStore, tmp_path: Path
) -> None:
    db = tmp_path / "nested" / "tickets.db"
    old, recent = await make_result(text=NAME_DETAIL), await make_result(text=NAME_DETAIL)
    store.save(old)
    store.save(recent)
    age_row(db, old.ticket_id, days=100)

    changed = store.purge_text_before(datetime.now(UTC) - timedelta(days=90))

    assert changed == 1
    purged = store.get(old.ticket_id)
    assert purged is not None
    assert (purged.redacted_text, purged.reasoning) == (PURGED_TEXT, PURGED_TEXT)
    assert (
        purged.model_copy(update={"redacted_text": old.redacted_text, "reasoning": old.reasoning})
        == old
    )  # everything else is kept
    assert store.get(recent.ticket_id) == recent


async def test_purge_is_idempotent(store: TicketStore, tmp_path: Path) -> None:
    result = await make_result()
    store.save(result)
    age_row(tmp_path / "nested" / "tickets.db", result.ticket_id, days=100)
    cutoff = datetime.now(UTC) - timedelta(days=90)

    assert store.purge_text_before(cutoff) == 1
    assert store.purge_text_before(cutoff) == 0


async def test_purged_and_deleted_text_is_gone_from_the_file(
    store: TicketStore, tmp_path: Path
) -> None:
    db = tmp_path / "nested" / "tickets.db"
    erased, deleted = await make_result(text=NAME_DETAIL), await make_result(text="Box wet")
    store.save(erased)
    store.save(deleted)
    for ticket_id in (erased.ticket_id, deleted.ticket_id):
        age_row(db, ticket_id, days=100)
    assert NAME_DETAIL.encode() in db.read_bytes()

    store.purge_text_before(datetime.now(UTC) - timedelta(days=90))
    assert NAME_DETAIL.encode() not in db.read_bytes()

    assert store.delete_before(datetime.now(UTC) - timedelta(days=90)) == 2
    assert deleted.ticket_id.encode() not in db.read_bytes()


async def test_delete_removes_only_old_rows(store: TicketStore, tmp_path: Path) -> None:
    old, recent = await make_result(), await make_result()
    store.save(old)
    store.save(recent)
    age_row(tmp_path / "nested" / "tickets.db", old.ticket_id, days=100)

    assert store.delete_before(datetime.now(UTC) - timedelta(days=90)) == 1
    assert store.get(old.ticket_id) is None
    assert store.get(recent.ticket_id) == recent


async def test_purged_fallbacks_are_not_offered_for_reprocessing(
    store: TicketStore, tmp_path: Path
) -> None:
    purged, pending = await make_result(ok=False), await make_result(ok=False)
    store.save(purged)
    store.save(pending)
    age_row(tmp_path / "nested" / "tickets.db", purged.ticket_id, days=100)
    store.purge_text_before(datetime.now(UTC) - timedelta(days=90))

    assert [r.ticket_id for r in store.list_fallbacks()] == [pending.ticket_id]
