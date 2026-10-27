import sqlite3
from pathlib import Path

import pytest

from tests.fakes import FakeLLM, response_with
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.llm import LLMResponse, LLMUnavailableError
from ticket_classifier.models import ClassificationStatus, PiiType, TicketResult
from ticket_classifier.service import StorageDisabledError, TicketService
from ticket_classifier.storage import TicketStore


@pytest.fixture
def settings() -> Settings:
    return Settings(max_attempts=1, _env_file=None)


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    return TicketStore(tmp_path / "tickets.db")


def service_for(
    settings: Settings, store: TicketStore | None, script: list[LLMResponse | BaseException]
) -> tuple[TicketService, FakeLLM]:
    llm = FakeLLM(script)
    return TicketService(TicketClassifier(llm, settings), store), llm


async def test_saves_every_result(settings: Settings, store: TicketStore) -> None:
    service, _ = service_for(settings, store, [response_with()])

    result = await service.classify("Where is my parcel?")

    assert await service.get(result.ticket_id) == result


async def test_repeat_external_id_returns_stored_result_without_calling_claude(
    settings: Settings, store: TicketStore
) -> None:
    service, llm = service_for(settings, store, [response_with()])

    first = await service.classify("Where is my parcel?", external_id="ZD-1")
    again = await service.classify("Where is my parcel?", external_id="ZD-1")

    assert len(llm.calls) == 1
    assert again == first
    assert first.external_id == "ZD-1"


async def test_repeat_of_a_fallback_is_classified_again_under_the_same_id(
    settings: Settings, store: TicketStore
) -> None:
    service, llm = service_for(settings, store, [LLMUnavailableError("down"), response_with()])

    failed = await service.classify("Where is my parcel?", external_id="ZD-2")
    retried = await service.classify("Where is my parcel?", external_id="ZD-2")

    assert len(llm.calls) == 2
    assert failed.status is ClassificationStatus.FALLBACK
    assert retried.status is ClassificationStatus.CLASSIFIED
    assert retried.ticket_id == failed.ticket_id
    assert await service.get(failed.ticket_id) == retried


async def test_reprocess_reclassifies_fallbacks_and_keeps_their_identity(
    settings: Settings, store: TicketStore
) -> None:
    service, llm = service_for(settings, store, [LLMUnavailableError("down"), response_with()])
    failed = await service.classify("Late parcel, email jane@example.com", external_id="ZD-3")

    outcomes = await service.reprocess_fallbacks()

    [(before, after)] = outcomes
    assert before == failed
    assert after.status is ClassificationStatus.CLASSIFIED
    assert (after.ticket_id, after.external_id) == (failed.ticket_id, "ZD-3")
    assert after.pii_redacted == [PiiType.EMAIL]  # kept from the original run
    assert "[EMAIL]" in llm.calls[1].user_message  # re-run uses the redacted text
    assert store.get(failed.ticket_id) == after
    assert store.list_fallbacks() == []


async def test_works_without_storage(settings: Settings) -> None:
    service, llm = service_for(settings, None, [response_with(), response_with()])

    await service.classify("Where is my parcel?", external_id="ZD-4")
    await service.classify("Where is my parcel?", external_id="ZD-4")

    assert len(llm.calls) == 2  # no store, so no idempotency
    with pytest.raises(StorageDisabledError):
        await service.get("anything")
    with pytest.raises(StorageDisabledError):
        await service.reprocess_fallbacks()


class BrokenStore(TicketStore):
    def save(self, result: TicketResult) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def get_by_external_id(self, external_id: str) -> TicketResult | None:
        raise sqlite3.OperationalError("disk I/O error")


async def test_storage_errors_are_logged_and_the_result_still_returned(
    settings: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    service, _ = service_for(settings, BrokenStore(tmp_path / "t.db"), [response_with()])

    result = await service.classify("Where is my parcel?", external_id="ZD-5")

    assert result.status is ClassificationStatus.CLASSIFIED
    assert f"ticket={result.ticket_id} could not be stored" in caplog.text
    assert "lookup failed for external_id=ZD-5" in caplog.text


class FirstLookupMisses(TicketStore):
    """Same database, but the first external_id lookup runs before the winner is saved."""

    lookups = 0

    def get_by_external_id(self, external_id: str) -> TicketResult | None:
        self.lookups += 1
        return None if self.lookups == 1 else super().get_by_external_id(external_id)


async def test_concurrent_duplicate_returns_the_first_stored_result(
    settings: Settings, store: TicketStore, tmp_path: Path
) -> None:
    winner_service, _ = service_for(settings, store, [response_with()])
    loser_service, _ = service_for(
        settings, FirstLookupMisses(tmp_path / "tickets.db"), [response_with()]
    )
    winner = await winner_service.classify("Where is my parcel?", external_id="ZD-6")

    result = await loser_service.classify("Where is my parcel?", external_id="ZD-6")

    assert result == winner
