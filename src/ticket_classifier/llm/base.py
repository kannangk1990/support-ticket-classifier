from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    duration_ms: int
    api_equivalent_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    data: Mapping[str, Any]
    model: str
    usage: LLMUsage


class LLMError(Exception):
    """Base class for backend failures."""


class LLMUnavailableError(LLMError):
    """Transient failure (process crash, rate limit, timeout). Safe to retry."""


class LLMOutputError(LLMError):
    """The model answered, but not with usable structured output. Worth one retry."""


class LLMConfigurationError(LLMError):
    """The backend is set up wrongly (CLI missing, API credentials present). Never retried:
    the operator has to fix it."""


class StructuredLLM(Protocol):
    async def complete(
        self, *, system_prompt: str, user_message: str, schema: Mapping[str, Any]
    ) -> LLMResponse: ...
