"""The classification pipeline: redact -> guard -> classify (with retry) -> apply rules."""

import asyncio
import logging
import uuid
from typing import Final

from pydantic import ValidationError

from ticket_classifier.config import Settings
from ticket_classifier.guard import check_injection
from ticket_classifier.llm import (
    LLMError,
    LLMOutputError,
    LLMResponse,
    LLMUnavailableError,
    StructuredLLM,
)
from ticket_classifier.models import (
    ClassificationStatus,
    IssueCategory,
    LLMClassification,
    PiiType,
    Priority,
    ReviewReason,
    Sentiment,
    TicketResult,
    Usage,
    llm_output_schema,
)
from ticket_classifier.pii import redact_pii
from ticket_classifier.prompts import build_user_message, get_prompt
from ticket_classifier.routing import review_reasons, team_for

logger = logging.getLogger(__name__)

_FALLBACK_CATEGORY: Final = IssueCategory.OTHER


class TicketClassifier:
    def __init__(self, llm: StructuredLLM, settings: Settings) -> None:
        self._llm = llm
        self._settings = settings
        self._prompt = get_prompt(settings.prompt_version)
        self._schema = llm_output_schema()
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def classify(self, ticket_text: str) -> TicketResult:
        """Classify one ticket.

        Never returns a partially trusted result: if Claude cannot classify the ticket,
        the result is a safe fallback routed to a human. LLMConfigurationError is not
        caught, because a misconfigured backend must be fixed, not silently masked.
        """
        ticket_id = uuid.uuid4().hex
        redaction = redact_pii(ticket_text)
        pii = list(redaction.found)
        # Step logs carry only IDs, PII types and sizes, so DEBUG output is safe to share.
        logger.debug(
            "ticket=%s received chars=%d redacted pii=%s",
            ticket_id,
            len(ticket_text),
            ",".join(pii) or "none",
        )

        guard = check_injection(redaction.text)
        if guard.flagged:
            logger.warning("ticket=%s flagged by guard pattern=%s", ticket_id, guard.pattern)
            return self._fallback(
                ticket_id,
                status=ClassificationStatus.FLAGGED_INJECTION,
                reason=ReviewReason.INJECTION_SUSPECTED,
                reasoning="Ticket matched a prompt-injection pattern; routed to a human.",
                pii=pii,
                redacted_text=redaction.text,
                attempts=0,
            )
        logger.debug("ticket=%s guard passed", ticket_id)

        try:
            classification, response, attempts = await self._classify_with_retry(
                ticket_id, redaction.text
            )
        except (LLMUnavailableError, LLMOutputError):
            logger.exception("ticket=%s classification failed; using fallback", ticket_id)
            return self._fallback(
                ticket_id,
                status=ClassificationStatus.FALLBACK,
                reason=ReviewReason.CLASSIFIER_UNAVAILABLE,
                reasoning="Automatic classification failed; routed to a human.",
                pii=pii,
                redacted_text=redaction.text,
                attempts=self._settings.max_attempts,
            )

        reasons = review_reasons(classification, self._settings.review_confidence_threshold)
        logger.info(
            "ticket=%s category=%s priority=%s review=%s attempts=%d pii=%s",
            ticket_id,
            classification.issue_category,
            classification.priority,
            bool(reasons),
            attempts,
            ",".join(pii) or "none",
        )
        return TicketResult(
            ticket_id=ticket_id,
            status=ClassificationStatus.CLASSIFIED,
            issue_category=classification.issue_category,
            assigned_team=team_for(classification.issue_category),
            priority=classification.priority,
            sentiment=classification.sentiment,
            confidence=classification.confidence,
            reasoning=classification.reasoning,
            requires_human_review=bool(reasons),
            review_reasons=reasons,
            pii_redacted=pii,
            redacted_text=redaction.text,
            prompt_version=self._prompt.version,
            model=response.model,
            attempts=attempts,
            usage=Usage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                duration_ms=response.usage.duration_ms,
                api_equivalent_cost_usd=response.usage.api_equivalent_cost_usd,
            ),
        )

    async def _classify_with_retry(
        self, ticket_id: str, redacted_text: str
    ) -> tuple[LLMClassification, LLMResponse, int]:
        user_message = build_user_message(redacted_text)
        last_error: LLMError | None = None

        for attempt in range(1, self._settings.max_attempts + 1):
            logger.debug("ticket=%s attempt %d started", ticket_id, attempt)
            try:
                async with self._semaphore, asyncio.timeout(self._settings.llm_timeout_seconds):
                    response = await self._llm.complete(
                        system_prompt=self._prompt.system_prompt,
                        user_message=user_message,
                        schema=self._schema,
                    )
                classification = LLMClassification.model_validate(response.data)
                logger.debug(
                    "ticket=%s attempt %d succeeded duration_ms=%d tokens_in=%d tokens_out=%d",
                    ticket_id,
                    attempt,
                    response.usage.duration_ms,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                )
                return classification, response, attempt
            except TimeoutError:
                last_error = LLMUnavailableError(
                    f"Timed out after {self._settings.llm_timeout_seconds}s"
                )
            except ValidationError as exc:
                last_error = LLMOutputError(f"Output failed validation: {exc.error_count()} errors")
            except (LLMUnavailableError, LLMOutputError) as exc:
                last_error = exc
            logger.warning("ticket=%s attempt %d failed: %s", ticket_id, attempt, last_error)

        assert last_error is not None  # noqa: S101 - loop runs at least once (max_attempts >= 1)
        raise last_error

    def _fallback(
        self,
        ticket_id: str,
        *,
        status: ClassificationStatus,
        reason: ReviewReason,
        reasoning: str,
        pii: list[PiiType],
        redacted_text: str,
        attempts: int,
    ) -> TicketResult:
        return TicketResult(
            ticket_id=ticket_id,
            status=status,
            issue_category=_FALLBACK_CATEGORY,
            assigned_team=team_for(_FALLBACK_CATEGORY),
            priority=Priority.MEDIUM,
            sentiment=Sentiment.NEUTRAL,
            confidence=0.0,
            reasoning=reasoning,
            requires_human_review=True,
            review_reasons=[reason],
            pii_redacted=pii,
            redacted_text=redacted_text,
            prompt_version=self._prompt.version,
            model=None,
            attempts=attempts,
            usage=None,
        )
