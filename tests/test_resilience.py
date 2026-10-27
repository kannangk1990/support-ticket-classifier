from ticket_classifier.resilience import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def state_of(circuit: CircuitBreaker) -> CircuitState:
    # A function call, so mypy doesn't assume the state is unchanged between asserts.
    return circuit.state


def breaker(clock: FakeClock, threshold: int = 2) -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=threshold, reset_seconds=30, clock=clock)


def test_opens_after_consecutive_failures() -> None:
    circuit = breaker(FakeClock())

    circuit.record_failure()
    assert state_of(circuit) is CircuitState.CLOSED
    circuit.record_failure()

    assert state_of(circuit) is CircuitState.OPEN
    assert not circuit.allow_request()


def test_success_resets_the_failure_count() -> None:
    circuit = breaker(FakeClock())

    circuit.record_failure()
    circuit.record_success()
    circuit.record_failure()

    assert state_of(circuit) is CircuitState.CLOSED


def test_allows_a_single_trial_after_the_cool_down() -> None:
    clock = FakeClock()
    circuit = breaker(clock)
    circuit.record_failure()
    circuit.record_failure()

    clock.now = 30
    assert state_of(circuit) is CircuitState.HALF_OPEN
    assert circuit.allow_request()
    assert not circuit.allow_request()  # only one trial at a time


def test_successful_trial_closes_the_circuit() -> None:
    clock = FakeClock()
    circuit = breaker(clock)
    circuit.record_failure()
    circuit.record_failure()
    clock.now = 30
    circuit.allow_request()

    circuit.record_success()

    assert state_of(circuit) is CircuitState.CLOSED
    assert circuit.allow_request()


def test_failed_trial_restarts_the_cool_down() -> None:
    clock = FakeClock()
    circuit = breaker(clock)
    circuit.record_failure()
    circuit.record_failure()
    clock.now = 30
    circuit.allow_request()

    circuit.record_failure()

    assert state_of(circuit) is CircuitState.OPEN
    clock.now = 59
    assert state_of(circuit) is CircuitState.OPEN
    clock.now = 60
    assert circuit.allow_request()
