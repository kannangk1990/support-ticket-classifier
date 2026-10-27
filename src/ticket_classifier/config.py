"""Runtime settings, read from environment variables prefixed with ``TICKET_`` (or a .env file)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TICKET_", env_file=".env", extra="ignore")

    claude_model: str = "claude-opus-5-5"
    # Classification is a short, well-specified task, so default to low effort to keep
    # latency and subscription usage down. Raise it only if the eval shows a gain.
    effort: Effort = "low"
    # v2 beat v1 on the 2026-10-27 eval (priority 54/54 vs 52/54, nothing worse).
    prompt_version: str = "v2"

    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=5)
    # Subscription plans are rate limited, so cap parallel Claude sessions.
    max_concurrency: int = Field(default=2, ge=1, le=10)
    review_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # Backoff before retrying a transient failure: a random wait up to base * 2^(n-1),
    # capped, so parallel tickets don't retry in lockstep against a struggling backend.
    retry_base_delay_seconds: float = Field(default=1.0, ge=0.0)
    retry_max_delay_seconds: float = Field(default=10.0, ge=0.0)
    # After this many tickets in a row fail to reach Claude, stop calling it for
    # circuit_reset_seconds and send tickets straight to human review.
    circuit_failure_threshold: int = Field(default=5, ge=1)
    circuit_reset_seconds: float = Field(default=30.0, gt=0)

    # In-app alerts over a sliding window of the most recent tickets.
    alert_window_tickets: int = Field(default=20, ge=1)
    alert_fallback_rate: float = Field(default=0.2, gt=0.0, le=1.0)
    alert_injection_rate: float = Field(default=0.3, gt=0.0, le=1.0)

    # Classification results, including redacted ticket text. Never the original text.
    db_path: Path = Path("data/tickets.db")

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
