"""Tests for the subscription backend. The SDK's query() is replaced, so nothing here
starts Claude Code or uses your subscription."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import CLINotFoundError, ProcessError, ResultMessage, SystemMessage

from tests.fakes import VALID_OUTPUT
from ticket_classifier.llm import (
    LLMConfigurationError,
    LLMOutputError,
    LLMUnavailableError,
    claude_code,
)
from ticket_classifier.llm.claude_code import ClaudeCodeClient, ensure_subscription_auth


def init_message(api_key_source: str = "none") -> SystemMessage:
    return SystemMessage(subtype="init", data={"apiKeySource": api_key_source})


def result_message(**overrides: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1200,
        "duration_api_ms": 1100,
        "is_error": False,
        "num_turns": 2,
        "session_id": "session",
        "total_cost_usd": 0.01,
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 900, "output_tokens": 50},
        "structured_output": dict(VALID_OUTPUT),
    }
    fields.update(overrides)
    return ResultMessage(**fields)


@pytest.fixture(autouse=True)
def subscription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in claude_code.NON_SUBSCRIPTION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def fake_query(monkeypatch: pytest.MonkeyPatch, *messages: object) -> list[Any]:
    captured: list[Any] = []

    async def query(*, prompt: str, options: Any) -> AsyncIterator[object]:
        captured.append(options)
        for message in messages:
            if isinstance(message, BaseException):
                raise message
            yield message

    monkeypatch.setattr(claude_code, "query", query)
    return captured


async def complete() -> Any:
    client = ClaudeCodeClient(model="claude-opus-5-5", effort="low")
    return await client.complete(system_prompt="sys", user_message="msg", schema={"type": "object"})


@pytest.mark.parametrize("name", claude_code.NON_SUBSCRIPTION_ENV_VARS)
def test_refuses_api_credentials(name: str) -> None:
    with pytest.raises(LLMConfigurationError, match=name):
        ensure_subscription_auth({name: ""})  # even an empty value takes precedence


def test_allows_clean_environment() -> None:
    ensure_subscription_auth({"PATH": "/usr/bin"})


async def test_success_maps_output_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = fake_query(monkeypatch, init_message(), result_message())

    response = await complete()

    assert response.data == VALID_OUTPUT
    assert response.usage.input_tokens == 903
    assert response.usage.output_tokens == 50
    options = captured[0]
    assert options.tools == []
    assert options.setting_sources == []
    assert options.output_format["type"] == "json_schema"


async def test_refuses_when_claude_code_reports_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_query(monkeypatch, init_message("ANTHROPIC_API_KEY"), result_message())

    with pytest.raises(LLMConfigurationError, match="apiKeySource"):
        await complete()


async def test_does_not_start_when_api_key_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = fake_query(monkeypatch, init_message(), result_message())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    with pytest.raises(LLMConfigurationError):
        await complete()
    assert captured == []


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (result_message(structured_output=None), LLMOutputError),
        (
            result_message(is_error=True, subtype="error_max_structured_output_retries"),
            LLMOutputError,
        ),
        (result_message(is_error=True, stop_reason="refusal"), LLMOutputError),
        (result_message(is_error=True, subtype="error_during_execution"), LLMUnavailableError),
    ],
)
async def test_maps_unusable_results(
    monkeypatch: pytest.MonkeyPatch, result: ResultMessage, error: type[Exception]
) -> None:
    fake_query(monkeypatch, init_message(), result)

    with pytest.raises(error):
        await complete()


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (CLINotFoundError(), LLMConfigurationError),
        (ProcessError("boom", exit_code=1), LLMUnavailableError),
    ],
)
async def test_maps_sdk_exceptions(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, error: type[Exception]
) -> None:
    fake_query(monkeypatch, init_message(), raised)

    with pytest.raises(error):
        await complete()


async def test_missing_result_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_query(monkeypatch, init_message())

    with pytest.raises(LLMUnavailableError):
        await complete()
