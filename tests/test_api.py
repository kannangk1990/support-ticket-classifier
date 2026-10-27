from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from tests.fakes import FakeLLM, response_with
from ticket_classifier.api import create_app
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.llm import LLMConfigurationError, LLMResponse


def client_for(script: list[LLMResponse | BaseException]) -> Iterator[TestClient]:
    settings = Settings(_env_file=None)
    classifier = TicketClassifier(FakeLLM(script), settings)
    with TestClient(create_app(settings, classifier)) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from client_for([response_with()])


def test_classify_returns_routed_result(client: TestClient) -> None:
    response = client.post("/api/v1/classify", json={"ticket_text": "My parcel is late."})

    assert response.status_code == 200
    body = response.json()
    assert body["issue_category"] == "delivery_issue"
    assert body["assigned_team"] == "logistics_team"
    assert body["status"] == "classified"


@pytest.mark.parametrize(
    "payload",
    [{"ticket_text": "hi"}, {"ticket_text": "x" * 5001}, {}, {"ticket_text": "ok ok", "x": 1}],
)
def test_rejects_invalid_requests(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post("/api/v1/classify", json=payload).status_code == 422


def test_misconfigured_backend_returns_503_without_details() -> None:
    for client in client_for([LLMConfigurationError("ANTHROPIC_API_KEY is set")]):
        response = client.post("/api/v1/classify", json={"ticket_text": "My parcel is late."})

        assert response.status_code == 503
        assert "ANTHROPIC_API_KEY" not in response.text


def test_health_and_index(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    index = client.get("/")
    assert index.status_code == 200
    assert "Support ticket classifier" in index.text


def test_response_includes_the_redacted_text() -> None:
    for client in client_for([response_with()]):
        response = client.post(
            "/api/v1/classify", json={"ticket_text": "Parcel late, email jane@example.com."}
        )

        body = response.json()
        assert body["redacted_text"] == "Parcel late, email [EMAIL]."
        assert "jane@example.com" not in response.text


def test_index_shows_the_text_sent_to_claude(client: TestClient) -> None:
    index = client.get("/").text

    assert "Sent to Claude" in index
    assert "result.redacted_text" in index
