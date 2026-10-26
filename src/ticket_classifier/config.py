"""Runtime settings, read from environment variables prefixed with ``TICKET_`` (or a .env file)."""

from functools import lru_cache
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
    prompt_version: str = "v1"

    llm_timeout_seconds: float = Field(default=120.0, gt=0)
    max_attempts: int = Field(default=2, ge=1, le=5)
    # Subscription plans are rate limited, so cap parallel Claude sessions.
    max_concurrency: int = Field(default=2, ge=1, le=10)
    review_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: LogLevel = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
