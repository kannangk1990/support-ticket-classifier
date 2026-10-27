"""Circuit breaker that stops calling Claude while it is unreachable.

Without it, every ticket during an outage waits for all its attempts to time out before
falling back. With it, after a run of failures tickets fall back immediately, and a single
trial ticket probes for recovery once the cool-down has passed.
"""

import time
from collections.abc import Callable
from enum import StrEnum


class CircuitState(StrEnum):
    CLOSED = "closed"  # normal: calls go through
    OPEN = "open"  # failing: calls are skipped until the cool-down ends
    HALF_OPEN = "half_open"  # cool-down over: one trial call decides what happens next


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        reset_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self._reset_seconds:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def allow_request(self) -> bool:
        state = self.state
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        """Claude was reachable, even if its answer was unusable."""
        self._consecutive_failures = 0
        self._opened_at = None
        self._trial_in_flight = False

    def record_failure(self) -> None:
        """Claude could not be reached for this ticket after all attempts."""
        self._trial_in_flight = False
        if self._opened_at is not None:
            # A failed trial (or a straggler finishing late) restarts the cool-down.
            self._opened_at = self._clock()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = self._clock()
