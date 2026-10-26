"""LLM backends. The classifier depends only on the StructuredLLM protocol."""

from ticket_classifier.llm.base import (
    LLMConfigurationError,
    LLMError,
    LLMOutputError,
    LLMResponse,
    LLMUnavailableError,
    LLMUsage,
    StructuredLLM,
)

__all__ = [
    "LLMConfigurationError",
    "LLMError",
    "LLMOutputError",
    "LLMResponse",
    "LLMUnavailableError",
    "LLMUsage",
    "StructuredLLM",
]
