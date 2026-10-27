"""The classification pipeline: redact -> guard -> classify (with retry) -> apply rules."""

import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import ValidationError

from ticket_classifier.config import Settings
from ticket_classifier.guard import check_injection
from ticket_classifier.llm import (
    LLMConfigurationError,
    LLMError,
    LLMOutputError,
    LLMRefusalError,
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
from ticket_classifier.prompts import build_retry_note, build_user_message, get_prompt
from ticket_classifier.resilience import CircuitBreaker
from ticket_classifier.routing import review_reasons, team_for

logger = logging.getLogger(__name__)

_FALLBACK_CATEGORY: Final = IssueCategory.OTHER


class _ClassificationFailedError(Exception):
    """Every allowed attempt failed; carries the last error and how many attempts ran."""

    def __init__(self, last_error: LLMError, attempts: int) -> None:
        super().__init__(str(last_error))
        self.last_error = last_error
        self.attempts = attempts


class TicketClassifier:
    def __init__(
        self,
        llm: StructuredLLM,
        settings: Settings,
        *,
        circuit_breaker: CircuitBreaker | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._llm = llm
        self._settings = settings
        self._prompt = get_prompt(settings.prompt_version)
        self._schema = llm_output_schema()
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._sleep = sleep
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=settings.circuit_failure_threshold,
            reset_seconds=settings.circuit_reset_seconds,
        )

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

        if not self.circuit_breaker.allow_request():
            logger.warning("ticket=%s circuit open; skipping Claude", ticket_id)
            return self._fallback(
                ticket_id,
                status=ClassificationStatus.FALLBACK,
                reason=ReviewReason.CLASSIFIER_UNAVAILABLE,
                reasoning="Classifier is temporarily unavailable; routed to a human.",
                pii=pii,
                redacted_text=redaction.text,
                attempts=0,
            )

        try:
            classification, response, attempts = await self._classify_with_retry(
                ticket_id, redaction.text
            )
        except _ClassificationFailedError as failure:
            # Only an unreachable backend counts against the circuit; an unusable answer
            # still proves Claude is up.
            if isinstance(failure.last_error, LLMUnavailableError):
                self.circuit_breaker.record_failure()
            else:
                self.circuit_breaker.record_success()
            logger.exception(
                "ticket=%s classification failed after %d attempt(s); using fallback: %s",
                ticket_id,
                failure.attempts,
                failure.last_error,
            )
            return self._fallback(
                ticket_id,
                status=ClassificationStatus.FALLBACK,
                reason=ReviewReason.CLASSIFIER_UNAVAILABLE,
                reasoning="Automatic classification failed; routed to a human.",
                pii=pii,
                redacted_text=redaction.text,
                attempts=failure.attempts,
            )
        except LLMConfigurationError:
            # Release a half-open trial slot; the error itself is re-raised to the caller.
            self.circuit_breaker.record_failure()
            raise
        self.circuit_breaker.record_success()

        reasons = review_reasons(classification, self._settings.review_confidence_threshold)
        reasoning = redact_pii(classification.reasoning)
        if reasoning.pii_detected:
            logger.warning(
                "ticket=%s personal data in model reasoning redacted pii=%s",
                ticket_id,
                ",".join(reasoning.found),
            )
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
            reasoning=reasoning.text,
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
        base_message = build_user_message(redacted_text)
        user_message = base_message
        last_error: LLMError | None = None

        for attempt in range(1, self._settings.max_attempts + 1):
            if attempt > 1 and isinstance(last_error, LLMUnavailableError):
                await self._backoff(ticket_id, attempt)
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
                user_message = f"{base_message}\n\n{build_retry_note(_invalid_fields(exc))}"
            except LLMRefusalError as exc:
                logger.warning("ticket=%s attempt %d refused; not retrying", ticket_id, attempt)
                raise _ClassificationFailedError(exc, attempt) from exc
            except LLMOutputError as exc:
                last_error = exc
                user_message = f"{base_message}\n\n{build_retry_note([])}"
            except LLMUnavailableError as exc:
                last_error = exc
            logger.warning("ticket=%s attempt %d failed: %s", ticket_id, attempt, last_error)

        assert last_error is not None  # noqa: S101 - loop runs at least once (max_attempts >= 1)
        raise _ClassificationFailedError(last_error, self._settings.max_attempts) from last_error

    async def _backoff(self, ticket_id: str, attempt: int) -> None:
        ceiling = min(
            self._settings.retry_max_delay_seconds,
            self._settings.retry_base_delay_seconds * 2 ** (attempt - 2),
        )
        # "Full jitter": spreads retries out so parallel tickets don't hit the backend
        # together. Not security-sensitive, so the standard PRNG is fine.
        delay = random.uniform(0, ceiling)  # noqa: S311
        logger.debug("ticket=%s backing off %.2fs before attempt %d", ticket_id, delay, attempt)
        await self._sleep(delay)

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


def _invalid_fields(error: ValidationError) -> list[str]:
    # Only report names from our own schema: an unexpected key was invented by the model,
    # so echoing it back is pointless and could carry arbitrary text.
    known = set(LLMClassification.model_fields)
    names = {str(item["loc"][0]) for item in error.errors() if item["loc"]}
    return sorted({name if name in known else "unexpected fields" for name in names})
