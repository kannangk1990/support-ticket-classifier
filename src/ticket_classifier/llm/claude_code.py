"""Claude backend that runs through Claude Code (the Claude Agent SDK) signed in with a
Claude subscription. It deliberately refuses to run on API-key or cloud-provider
credentials, so the project can never bill the Anthropic API by accident."""

import logging
import os
from collections.abc import Mapping
from typing import Any, Final

from claude_agent_sdk import (
    ClaudeAgentOptions,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    ResultError,
    ResultMessage,
    SystemMessage,
    query,
)

from ticket_classifier.config import Effort
from ticket_classifier.llm.base import (
    LLMConfigurationError,
    LLMOutputError,
    LLMResponse,
    LLMUnavailableError,
    LLMUsage,
)

logger = logging.getLogger(__name__)

# Any of these makes Claude Code authenticate against a paid API instead of the
# subscription login. Even an empty ANTHROPIC_API_KEY takes precedence.
NON_SUBSCRIPTION_ENV_VARS: Final = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)
_SUBSCRIPTION_KEY_SOURCE: Final = "none"
# Structured output takes two turns: one to call the StructuredOutput tool, one to finish.
_MAX_TURNS: Final = 3


def ensure_subscription_auth(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    present = [name for name in NON_SUBSCRIPTION_ENV_VARS if name in env]
    if present:
        raise LLMConfigurationError(
            "Refusing to call Claude: "
            + ", ".join(present)
            + " is set, which would bill the API instead of your Claude subscription. "
            "Unset it and sign in with `claude` (/login) instead."
        )


class ClaudeCodeClient:
    def __init__(self, *, model: str, effort: Effort) -> None:
        self._model = model
        self._effort = effort

    async def complete(
        self, *, system_prompt: str, user_message: str, schema: Mapping[str, Any]
    ) -> LLMResponse:
        ensure_subscription_auth()
        options = ClaudeAgentOptions(
            model=self._model,
            effort=self._effort,
            system_prompt=system_prompt,
            output_format={"type": "json_schema", "schema": dict(schema)},
            tools=[],  # classification needs no tools; this also blocks file and shell access
            setting_sources=[],  # ignore the user's CLAUDE.md, hooks and settings
            max_turns=_MAX_TURNS,
        )

        result: ResultMessage | None = None
        try:
            async for message in query(prompt=user_message, options=options):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    _check_key_source(message.data)
                elif isinstance(message, ResultMessage):
                    result = message
        except CLINotFoundError as exc:
            raise LLMConfigurationError(
                "Claude Code CLI not found. Reinstall dependencies with `uv sync`."
            ) from exc
        except ResultError as exc:
            raise _error_for_result(exc.subtype, exc.data.get("stop_reason")) from exc
        except (ProcessError, CLIConnectionError, CLIJSONDecodeError) as exc:
            raise LLMUnavailableError(f"Claude Code process failed: {type(exc).__name__}") from exc

        if result is None:
            raise LLMUnavailableError("Claude Code ended without a result message")
        if result.is_error:
            raise _error_for_result(result.subtype, result.stop_reason)
        if not isinstance(result.structured_output, dict):
            raise LLMOutputError("Claude returned no structured output")

        return LLMResponse(
            data=result.structured_output,
            model=self._model,
            usage=_usage_from(result),
        )


def _check_key_source(init_data: Mapping[str, Any]) -> None:
    # Second line of defence after ensure_subscription_auth: Claude Code reports which
    # credential it actually picked up, and "none" means the subscription login.
    source = init_data.get("apiKeySource")
    if source != _SUBSCRIPTION_KEY_SOURCE:
        raise LLMConfigurationError(
            f"Claude Code is using API credentials (apiKeySource={source!r}) instead of "
            "your subscription. Remove the API key configuration and sign in with `claude`."
        )


def _error_for_result(
    subtype: str | None, stop_reason: object
) -> LLMOutputError | LLMUnavailableError:
    if stop_reason == "refusal":
        return LLMOutputError("Claude declined to classify this ticket")
    if subtype in ("error_max_structured_output_retries", "error_max_turns"):
        return LLMOutputError(f"Claude did not produce valid structured output ({subtype})")
    return LLMUnavailableError(f"Claude Code run failed ({subtype})")


def _usage_from(result: ResultMessage) -> LLMUsage:
    usage: Mapping[str, Any] = result.usage or {}
    # Cached prompt tokens are reported separately; count them so totals reflect the work done.
    input_tokens = sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens") or 0),
        duration_ms=result.duration_ms,
        api_equivalent_cost_usd=result.total_cost_usd,
    )
