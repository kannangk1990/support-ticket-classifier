"""Prometheus metrics, served at /metrics.

Every label value comes from a fixed enum (status, category, review reason, PII type,
attempt outcome), so metrics can never carry ticket content and cardinality stays bounded.
A dedicated registry keeps these separate from anything else in the process.
"""

from enum import StrEnum
from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from ticket_classifier.models import TicketResult

REGISTRY: Final = CollectorRegistry()

TICKETS: Final = Counter(
    "ticket_classifier_tickets",
    "Tickets processed, by final status.",
    ["status"],
    registry=REGISTRY,
)
CATEGORIES: Final = Counter(
    "ticket_classifier_categories",
    "Tickets by assigned category and priority.",
    ["category", "priority"],
    registry=REGISTRY,
)
REVIEWS: Final = Counter(
    "ticket_classifier_review_reasons",
    "Reasons tickets were sent to human review.",
    ["reason"],
    registry=REGISTRY,
)
PII: Final = Counter(
    "ticket_classifier_pii_redacted",
    "Tickets in which each type of personal data was redacted.",
    ["type"],
    registry=REGISTRY,
)
ATTEMPTS: Final = Counter(
    "ticket_classifier_llm_attempts",
    "Calls to Claude, by outcome.",
    ["outcome"],
    registry=REGISTRY,
)
TOKENS: Final = Counter(
    "ticket_classifier_llm_tokens",
    "Tokens used by successful classifications.",
    ["direction"],
    registry=REGISTRY,
)
DURATION: Final = Histogram(
    "ticket_classifier_duration_seconds",
    "End-to-end time to classify a ticket, including retries.",
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300),
    registry=REGISTRY,
)
CIRCUIT_STATE: Final = Gauge(
    "ticket_classifier_circuit_state",
    "Circuit breaker state: 0 closed, 1 half-open, 2 open.",
    registry=REGISTRY,
)
STORAGE_ERRORS: Final = Counter(
    "ticket_classifier_storage_errors",
    "Failed reads or writes to the result store.",
    registry=REGISTRY,
)
STORED_HITS: Final = Counter(
    "ticket_classifier_stored_result_hits",
    "Repeat external_ids answered from storage without calling Claude.",
    registry=REGISTRY,
)
ALERT_FIRING: Final = Gauge(
    "ticket_classifier_alert_firing",
    "1 while an in-app alert is firing, else 0.",
    ["alert"],
    registry=REGISTRY,
)


class AttemptOutcome(StrEnum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    INVALID_OUTPUT = "invalid_output"
    REFUSED = "refused"


def record_result(result: TicketResult, duration_seconds: float) -> None:
    TICKETS.labels(status=result.status).inc()
    CATEGORIES.labels(category=result.issue_category, priority=result.priority).inc()
    for reason in result.review_reasons:
        REVIEWS.labels(reason=reason).inc()
    for pii_type in result.pii_redacted:
        PII.labels(type=pii_type).inc()
    if result.usage is not None:
        TOKENS.labels(direction="input").inc(result.usage.input_tokens)
        TOKENS.labels(direction="output").inc(result.usage.output_tokens)
    DURATION.observe(duration_seconds)
