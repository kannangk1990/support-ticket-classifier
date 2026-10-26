from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ticket_classifier.llm import LLMResponse, LLMUsage

VALID_OUTPUT: dict[str, Any] = {
    "issue_category": "delivery_issue",
    "priority": "high",
    "sentiment": "angry",
    "confidence": 0.93,
    "reasoning": "Customer reports a parcel that has not arrived.",
    "injection_suspected": False,
}


def response_with(**overrides: Any) -> LLMResponse:
    return LLMResponse(
        data={**VALID_OUTPUT, **overrides},
        model="fake-model",
        usage=LLMUsage(
            input_tokens=120, output_tokens=40, duration_ms=5, api_equivalent_cost_usd=0.001
        ),
    )


@dataclass
class Call:
    system_prompt: str
    user_message: str
    schema: Mapping[str, Any]


@dataclass
class FakeLLM:
    """Plays back scripted responses (or raises scripted exceptions) in order."""

    script: list[LLMResponse | BaseException]
    calls: list[Call] = field(default_factory=list)

    async def complete(
        self, *, system_prompt: str, user_message: str, schema: Mapping[str, Any]
    ) -> LLMResponse:
        self.calls.append(Call(system_prompt, user_message, schema))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step
