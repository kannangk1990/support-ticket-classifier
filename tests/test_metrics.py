import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from tests.fakes import FakeLLM, response_with
from ticket_classifier.api import create_app
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.llm import LLMRefusalError, LLMUnavailableError
from ticket_classifier.metrics import REGISTRY
from ticket_classifier.models import TicketResult
from ticket_classifier.service import TicketService
from ticket_classifier.storage import TicketStore

ALERT_RULES = Path(__file__).parent.parent / "deploy" / "prometheus" / "alerts.yml"


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def settings() -> Settings:
    return Settings(max_attempts=2, retry_base_delay_seconds=0, _env_file=None)


async def test_classified_ticket_updates_counters(settings: Settings) -> None:
    before = {
        "tickets": sample("ticket_classifier_tickets_total", status="classified"),
        "category": sample(
            "ticket_classifier_categories_total", category="delivery_issue", priority="high"
        ),
        "pii": sample("ticket_classifier_pii_redacted_total", type="email"),
        "success": sample("ticket_classifier_llm_attempts_total", outcome="success"),
        "tokens_in": sample("ticket_classifier_llm_tokens_total", direction="input"),
        "durations": sample("ticket_classifier_duration_seconds_count"),
    }

    await TicketClassifier(FakeLLM([response_with()]), settings).classify(
        "Parcel late, email jane@example.com"
    )

    assert sample("ticket_classifier_tickets_total", status="classified") == before["tickets"] + 1
    assert (
        sample("ticket_classifier_categories_total", category="delivery_issue", priority="high")
        == before["category"] + 1
    )
    assert sample("ticket_classifier_pii_redacted_total", type="email") == before["pii"] + 1
    assert (
        sample("ticket_classifier_llm_attempts_total", outcome="success") == before["success"] + 1
    )
    assert (
        sample("ticket_classifier_llm_tokens_total", direction="input") == before["tokens_in"] + 120
    )
    assert sample("ticket_classifier_duration_seconds_count") == before["durations"] + 1


async def test_failed_attempts_are_counted_by_outcome(settings: Settings) -> None:
    unavailable = sample("ticket_classifier_llm_attempts_total", outcome="unavailable")
    invalid = sample("ticket_classifier_llm_attempts_total", outcome="invalid_output")
    refused = sample("ticket_classifier_llm_attempts_total", outcome="refused")
    fallbacks = sample("ticket_classifier_tickets_total", status="fallback")

    await TicketClassifier(
        FakeLLM([LLMUnavailableError("down"), response_with(issue_category="bad")]), settings
    ).classify("Where is my parcel?")
    await TicketClassifier(FakeLLM([LLMRefusalError("no")]), settings).classify(
        "Where is my parcel?"
    )

    assert sample("ticket_classifier_llm_attempts_total", outcome="unavailable") == unavailable + 1
    assert sample("ticket_classifier_llm_attempts_total", outcome="invalid_output") == invalid + 1
    assert sample("ticket_classifier_llm_attempts_total", outcome="refused") == refused + 1
    assert sample("ticket_classifier_tickets_total", status="fallback") == fallbacks + 2


async def test_circuit_gauge_follows_the_breaker(settings: Settings) -> None:
    tuned = settings.model_copy(update={"max_attempts": 1, "circuit_failure_threshold": 1})
    classifier = TicketClassifier(FakeLLM([LLMUnavailableError("down")]), tuned)
    assert sample("ticket_classifier_circuit_state") == 0

    await classifier.classify("Where is my parcel?")

    assert sample("ticket_classifier_circuit_state") == 2


async def test_service_counts_stored_hits_and_storage_errors(
    settings: Settings, tmp_path: Path
) -> None:
    hits = sample("ticket_classifier_stored_result_hits_total")
    errors = sample("ticket_classifier_storage_errors_total")
    store = TicketStore(tmp_path / "t.db")
    service = TicketService(TicketClassifier(FakeLLM([response_with()]), settings), store)

    await service.classify("Where is my parcel?", external_id="ZD-1")
    await service.classify("Where is my parcel?", external_id="ZD-1")

    class BrokenStore(TicketStore):
        def save(self, result: TicketResult) -> None:
            raise sqlite3.OperationalError("disk full")

    broken = TicketService(
        TicketClassifier(FakeLLM([response_with()]), settings), BrokenStore(tmp_path / "b.db")
    )
    await broken.classify("Where is my parcel?")

    assert sample("ticket_classifier_stored_result_hits_total") == hits + 1
    assert sample("ticket_classifier_storage_errors_total") == errors + 1


async def test_metrics_never_contain_ticket_text(settings: Settings) -> None:
    await TicketClassifier(FakeLLM([response_with()]), settings).classify(
        "My neighbour Bob signed. Email jane@example.com"
    )

    exported = generate_latest(REGISTRY).decode()
    assert "jane@example.com" not in exported
    assert "neighbour Bob" not in exported


def test_metrics_endpoint_serves_prometheus_text(settings: Settings) -> None:
    classifier = TicketClassifier(FakeLLM([response_with()]), settings)
    with TestClient(create_app(settings, classifier)) as client:
        client.post("/api/v1/classify", json={"ticket_text": "My parcel is late."})
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert 'ticket_classifier_tickets_total{status="classified"}' in response.text


def _exported_names() -> set[str]:
    suffixes = {"counter": ["_total"], "histogram": ["_bucket", "_count", "_sum"], "gauge": [""]}
    return {
        metric.name + suffix
        for metric in REGISTRY.collect()
        for suffix in suffixes.get(metric.type, [""])
    }


def test_alert_rules_only_use_metrics_that_exist() -> None:
    used = set(re.findall(r"\bticket_classifier_[a-z_]+", ALERT_RULES.read_text()))

    assert used, "expected the rules to reference classifier metrics"
    assert used <= _exported_names(), f"unknown metrics: {used - _exported_names()}"
