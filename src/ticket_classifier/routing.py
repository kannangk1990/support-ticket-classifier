"""Deterministic business rules applied after Claude classifies a ticket."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ticket_classifier.models import IssueCategory, LLMClassification, Priority, ReviewReason, Team
from ticket_classifier.pii import redact_pii

CATEGORY_TEAM: Final[Mapping[IssueCategory, Team]] = MappingProxyType(
    {
        IssueCategory.ORDER: Team.FULFILLMENT,
        IssueCategory.PAYMENT: Team.PAYMENTS,
        IssueCategory.REFUND: Team.PAYMENTS,
        IssueCategory.DELIVERY: Team.LOGISTICS,
        IssueCategory.ACCOUNT: Team.TECH,
        IssueCategory.PRODUCT: Team.CUSTOMER_SUPPORT,
        IssueCategory.OTHER: Team.CUSTOMER_SUPPORT,
    }
)


def team_for(category: IssueCategory) -> Team:
    return CATEGORY_TEAM[category]


def review_reasons(
    classification: LLMClassification, confidence_threshold: float
) -> list[ReviewReason]:
    reasons: list[ReviewReason] = []
    if classification.injection_suspected:
        reasons.append(ReviewReason.INJECTION_SUSPECTED)
    if classification.confidence < confidence_threshold:
        reasons.append(ReviewReason.LOW_CONFIDENCE)
    if classification.priority is Priority.CRITICAL:
        reasons.append(ReviewReason.CRITICAL_PRIORITY)
    if classification.issue_category is IssueCategory.OTHER:
        reasons.append(ReviewReason.UNCATEGORISED)
    # Claude only ever sees redacted text, so personal data in its answer means a
    # pattern was missed or invented; a human should look before anyone acts on it.
    if redact_pii(classification.reasoning).pii_detected:
        reasons.append(ReviewReason.PII_IN_OUTPUT)
    return reasons
