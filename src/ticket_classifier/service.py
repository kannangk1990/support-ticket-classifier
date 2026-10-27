"""Ties the classifier to storage: idempotent classification, lookup and reprocessing.

The classifier stays storage-agnostic. Storage problems are logged and never turn a
successful classification into an error, because the Claude call has already been made.
"""

import asyncio
import logging
import sqlite3

from ticket_classifier import metrics
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.models import ClassificationStatus, TicketResult
from ticket_classifier.storage import DuplicateExternalIdError, TicketStore

logger = logging.getLogger(__name__)


class StorageDisabledError(Exception):
    """The service was built without a store."""


class TicketService:
    def __init__(self, classifier: TicketClassifier, store: TicketStore | None) -> None:
        self._classifier = classifier
        self._store = store

    async def classify(self, ticket_text: str, external_id: str | None = None) -> TicketResult:
        existing = await self._find_external(external_id)
        if existing is not None and existing.status is not ClassificationStatus.FALLBACK:
            metrics.STORED_HITS.inc()
            logger.info(
                "ticket=%s returning stored result external_id=%s",
                existing.ticket_id,
                external_id,
            )
            return existing

        result = await self._classifier.classify(ticket_text)
        update: dict[str, object] = {"external_id": external_id}
        if existing is not None:
            # Retrying a stored fallback: keep its identity so callers see one ticket.
            update["ticket_id"] = existing.ticket_id
        return await self._save(result.model_copy(update=update))

    async def get(self, ticket_id: str) -> TicketResult | None:
        if self._store is None:
            raise StorageDisabledError
        return await asyncio.to_thread(self._store.get, ticket_id)

    async def reprocess_fallbacks(
        self, limit: int | None = None
    ) -> list[tuple[TicketResult, TicketResult]]:
        """Re-run stored fallback tickets from their redacted text. Returns (before, after)."""
        if self._store is None:
            raise StorageDisabledError
        pending = await asyncio.to_thread(self._store.list_fallbacks, limit)
        fresh = await asyncio.gather(
            *(self._classifier.classify(stored.redacted_text) for stored in pending)
        )
        outcomes: list[tuple[TicketResult, TicketResult]] = []
        for stored, result in zip(pending, fresh, strict=True):
            # The stored text is already redacted, so a re-run finds no PII; keep the
            # original findings and identity.
            updated = result.model_copy(
                update={
                    "ticket_id": stored.ticket_id,
                    "external_id": stored.external_id,
                    "pii_redacted": stored.pii_redacted,
                }
            )
            outcomes.append((stored, await self._save(updated)))
            logger.info(
                "ticket=%s reprocessed status=%s->%s",
                stored.ticket_id,
                stored.status,
                updated.status,
            )
        return outcomes

    async def _find_external(self, external_id: str | None) -> TicketResult | None:
        if self._store is None or external_id is None:
            return None
        try:
            return await asyncio.to_thread(self._store.get_by_external_id, external_id)
        except sqlite3.Error:
            metrics.STORAGE_ERRORS.inc()
            logger.exception("lookup failed for external_id=%s; classifying again", external_id)
            return None

    async def _save(self, result: TicketResult) -> TicketResult:
        if self._store is None:
            return result
        try:
            await asyncio.to_thread(self._store.save, result)
        except DuplicateExternalIdError:
            # A concurrent request with the same external_id saved first; its answer wins
            # so every caller sees the same result.
            winner = await self._find_external(result.external_id)
            if winner is not None:
                logger.info(
                    "ticket=%s duplicate external_id=%s; returning ticket=%s",
                    result.ticket_id,
                    result.external_id,
                    winner.ticket_id,
                )
                return winner
            logger.error("ticket=%s duplicate external_id but no stored row", result.ticket_id)
        except sqlite3.Error:
            metrics.STORAGE_ERRORS.inc()
            logger.exception("ticket=%s could not be stored", result.ticket_id)
        return result
