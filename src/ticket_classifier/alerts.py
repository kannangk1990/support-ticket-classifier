"""In-app alerts, so problems surface in the logs even when no Prometheus is running.

Each alert logs once when it starts firing (ERROR) and once when it resolves (INFO), never
on every ticket. The same conditions exist as Prometheus rules in deploy/prometheus/.
"""

import logging
from collections import deque
from enum import StrEnum

from ticket_classifier.config import Settings
from ticket_classifier.metrics import ALERT_FIRING
from ticket_classifier.models import ClassificationStatus, TicketResult, injection_detected
from ticket_classifier.resilience import CircuitState

logger = logging.getLogger(__name__)


class Alert(StrEnum):
    HIGH_FALLBACK_RATE = "high_fallback_rate"
    HIGH_INJECTION_RATE = "high_injection_rate"
    CIRCUIT_OPEN = "circuit_open"


class AlertMonitor:
    def __init__(self, settings: Settings) -> None:
        self._window: deque[TicketResult] = deque(maxlen=settings.alert_window_tickets)
        self._fallback_threshold = settings.alert_fallback_rate
        self._injection_threshold = settings.alert_injection_rate
        self._firing: set[Alert] = set()

    @property
    def firing(self) -> frozenset[Alert]:
        return frozenset(self._firing)

    def observe(self, result: TicketResult, circuit: CircuitState) -> None:
        self._update(Alert.CIRCUIT_OPEN, circuit is CircuitState.OPEN, f"circuit is {circuit}")

        self._window.append(result)
        # Rates over a half-empty window are noise (1 failure in 2 tickets is not 50%).
        if len(self._window) < (self._window.maxlen or 0):
            return
        size = len(self._window)
        fallback_rate = sum(r.status is ClassificationStatus.FALLBACK for r in self._window) / size
        injection_rate = sum(injection_detected(r) for r in self._window) / size
        self._update(
            Alert.HIGH_FALLBACK_RATE,
            fallback_rate >= self._fallback_threshold,
            f"fallback rate {fallback_rate:.0%} over last {size} tickets "
            f"(threshold {self._fallback_threshold:.0%})",
        )
        self._update(
            Alert.HIGH_INJECTION_RATE,
            injection_rate >= self._injection_threshold,
            f"injection rate {injection_rate:.0%} over last {size} tickets "
            f"(threshold {self._injection_threshold:.0%})",
        )

    def _update(self, alert: Alert, active: bool, detail: str) -> None:
        if active and alert not in self._firing:
            self._firing.add(alert)
            ALERT_FIRING.labels(alert=alert).set(1)
            logger.error("ALERT FIRING %s: %s", alert, detail)
        elif not active and alert in self._firing:
            self._firing.discard(alert)
            ALERT_FIRING.labels(alert=alert).set(0)
            logger.info("ALERT RESOLVED %s: %s", alert, detail)
