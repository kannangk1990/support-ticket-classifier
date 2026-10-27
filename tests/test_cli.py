import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fakes import FakeLLM, response_with
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.cli import main
from ticket_classifier.config import Settings, get_settings
from ticket_classifier.storage import TicketStore


def test_reprocess_rejects_a_zero_limit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reprocess", "--limit", "0"]) == 2
    assert "--limit must be at least 1" in capsys.readouterr().err


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "tickets.db"
    monkeypatch.setenv("TICKET_DB_PATH", str(db))
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_purge_rejects_a_zero_age(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["purge", "--older-than-days", "0"]) == 2
    assert "--older-than-days must be at least 1" in capsys.readouterr().err


async def test_purge_command_erases_old_text_by_default(
    temp_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = TicketStore(temp_db)
    result = await TicketClassifier(FakeLLM([response_with()]), Settings(_env_file=None)).classify(
        "Parcel late"
    )
    store.save(result)
    with sqlite3.connect(temp_db) as conn:
        conn.execute("UPDATE tickets SET created_at = '2000-01-01T00:00:00+00:00'")

    assert main(["purge", "--older-than-days", "90"]) == 0
    assert "Erased text from 1 ticket(s) older than 90 day(s)." in capsys.readouterr().out
    assert store.get(result.ticket_id) is not None

    assert main(["purge", "--older-than-days", "90", "--delete"]) == 0
    assert "Deleted 1 ticket(s) older than 90 day(s)." in capsys.readouterr().out
    assert store.get(result.ticket_id) is None
