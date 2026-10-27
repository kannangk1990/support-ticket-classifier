import pytest

from ticket_classifier.alerts import Alert, AlertMonitor
from ticket_classifier.config import Settings
from ticket_classifier.metrics import REGISTRY
from ticket_classifier.models import (
    ClassificationStatus,
    IssueCategory,
    Priority,
    ReviewReason,
    Sentiment,
    Team,
    TicketResult,
)
from ticket_classifier.resilience import CircuitState

CLOSED = CircuitState.CLOSED


def result(
    status: ClassificationStatus = ClassificationStatus.CLASSIFIED,
    reasons: list[ReviewReason] | None = None,
) -> TicketResult:
    return TicketResult(
        ticket_id="t",
        status=status,
        issue_category=IssueCategory.DELIVERY,
        assigned_team=Team.LOGISTICS,
        priority=Priority.MEDIUM,
        sentiment=Sentiment.NEUTRAL,
        confidence=0.9,
        reasoning="r",
        requires_human_review=bool(reasons),
        review_reasons=reasons or [],
        pii_redacted=[],
        redacted_text="x",
        prompt_version="v1",
        model=None,
        attempts=1,
        usage=None,
    )


FALLBACK = result(ClassificationStatus.FALLBACK, [ReviewReason.CLASSIFIER_UNAVAILABLE])
INJECTION = result(ClassificationStatus.FLAGGED_INJECTION, [ReviewReason.INJECTION_SUSPECTED])


@pytest.fixture
def monitor() -> AlertMonitor:
    return AlertMonitor(
        Settings(
            alert_window_tickets=4,
            alert_fallback_rate=0.5,
            alert_injection_rate=0.5,
            _env_file=None,
        )
    )


def firing_gauge(alert: Alert) -> float:
    return REGISTRY.get_sample_value("ticket_classifier_alert_firing", {"alert": alert}) or 0.0


def test_waits_for_a_full_window_before_judging_rates(monitor: AlertMonitor) -> None:
    for _ in range(3):
        monitor.observe(FALLBACK, CLOSED)

    assert monitor.firing == frozenset()


def test_fallback_alert_fires_once_and_resolves(
    monitor: AlertMonitor, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="ticket_classifier.alerts")
    for _ in range(6):
        monitor.observe(FALLBACK, CLOSED)

    assert monitor.firing == {Alert.HIGH_FALLBACK_RATE}
    assert firing_gauge(Alert.HIGH_FALLBACK_RATE) == 1
    assert caplog.text.count("ALERT FIRING high_fallback_rate") == 1

    for _ in range(3):
        monitor.observe(result(), CLOSED)

    assert monitor.firing == frozenset()
    assert firing_gauge(Alert.HIGH_FALLBACK_RATE) == 0
    assert caplog.text.count("ALERT RESOLVED high_fallback_rate") == 1


def test_injection_alert_counts_guard_and_model_flags(monitor: AlertMonitor) -> None:
    model_flagged = result(reasons=[ReviewReason.INJECTION_SUSPECTED])
    for item in (INJECTION, model_flagged, result(), result()):
        monitor.observe(item, CLOSED)

    assert Alert.HIGH_INJECTION_RATE in monitor.firing


def test_circuit_alert_follows_the_breaker(
    monitor: AlertMonitor, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="ticket_classifier.alerts")
    monitor.observe(result(), CircuitState.OPEN)
    monitor.observe(result(), CircuitState.OPEN)
    assert monitor.firing == {Alert.CIRCUIT_OPEN}
    assert caplog.text.count("ALERT FIRING circuit_open") == 1

    monitor.observe(result(), CircuitState.HALF_OPEN)

    assert monitor.firing == frozenset()
    assert "ALERT RESOLVED circuit_open" in caplog.text
