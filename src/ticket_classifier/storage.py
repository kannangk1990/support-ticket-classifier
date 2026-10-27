"""SQLite store for classification results.

Each row holds the full TicketResult as JSON, which includes the redacted ticket text so
fallback tickets can be reprocessed. The original, unredacted text is never stored.

Redacted text can still hold personal data the regexes miss (names, addresses), so
purge_text_before() erases it, and Claude's reasoning, from old rows while keeping the
classification. secure_delete makes SQLite overwrite the old bytes instead of leaving them
in free pages of the file.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ticket_classifier.models import ClassificationStatus, TicketResult

SCHEMA_VERSION: Final = 1
PURGED_TEXT: Final = "[PURGED]"

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   TEXT PRIMARY KEY,
    external_id TEXT UNIQUE,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_status ON tickets (status, created_at);
"""


class DuplicateExternalIdError(Exception):
    """Another ticket already uses this external_id (two requests raced)."""


class TicketStore:
    """Synchronous on purpose: callers in async code run it via asyncio.to_thread, and a
    fresh connection per call keeps it safe across threads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=10)) as conn, conn:
            conn.execute("PRAGMA secure_delete = ON")
            yield conn

    def save(self, result: TicketResult) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            with self._transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO tickets
                        (ticket_id, external_id, status, created_at, updated_at, result_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (ticket_id) DO UPDATE SET
                        external_id = excluded.external_id,
                        status = excluded.status,
                        updated_at = excluded.updated_at,
                        result_json = excluded.result_json
                    """,
                    (
                        result.ticket_id,
                        result.external_id,
                        result.status.value,
                        now,
                        now,
                        result.model_dump_json(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateExternalIdError(result.external_id) from exc

    def get(self, ticket_id: str) -> TicketResult | None:
        return self._one("SELECT result_json FROM tickets WHERE ticket_id = ?", ticket_id)

    def get_by_external_id(self, external_id: str) -> TicketResult | None:
        return self._one("SELECT result_json FROM tickets WHERE external_id = ?", external_id)

    def list_fallbacks(self, limit: int | None = None) -> list[TicketResult]:
        """Fallback tickets that can still be reprocessed (their text has not been purged)."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT result_json FROM tickets WHERE status = ? ORDER BY created_at",
                (ClassificationStatus.FALLBACK.value,),
            ).fetchall()
        results = [TicketResult.model_validate_json(row[0]) for row in rows]
        pending = [r for r in results if r.redacted_text != PURGED_TEXT]
        return pending if limit is None else pending[:limit]

    def purge_text_before(self, cutoff: datetime) -> int:
        """Erase ticket text and Claude's reasoning from rows created before cutoff, keeping
        the classification. Returns how many rows changed; already purged rows don't count."""
        now = datetime.now(UTC).isoformat()
        changed = 0
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT ticket_id, result_json FROM tickets WHERE created_at < ?",
                (cutoff.astimezone(UTC).isoformat(),),
            ).fetchall()
            for ticket_id, raw in rows:
                result = TicketResult.model_validate_json(raw)
                if result.redacted_text == PURGED_TEXT and result.reasoning == PURGED_TEXT:
                    continue
                purged = result.model_copy(
                    update={"redacted_text": PURGED_TEXT, "reasoning": PURGED_TEXT}
                )
                conn.execute(
                    "UPDATE tickets SET result_json = ?, updated_at = ? WHERE ticket_id = ?",
                    (purged.model_dump_json(), now, ticket_id),
                )
                changed += 1
        return changed

    def delete_before(self, cutoff: datetime) -> int:
        """Delete whole rows created before cutoff. Returns how many were deleted."""
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM tickets WHERE created_at < ?", (cutoff.astimezone(UTC).isoformat(),)
            )
        return cursor.rowcount

    def _one(self, query: str, key: str) -> TicketResult | None:
        with self._transaction() as conn:
            row = conn.execute(query, (key,)).fetchone()
        return None if row is None else TicketResult.model_validate_json(row[0])
