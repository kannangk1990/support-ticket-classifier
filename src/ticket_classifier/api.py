"""HTTP API and demo page."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Final

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ticket_classifier import __version__
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings, get_settings
from ticket_classifier.llm import LLMConfigurationError
from ticket_classifier.llm.claude_code import ClaudeCodeClient, ensure_subscription_auth
from ticket_classifier.models import TicketResult

logger = logging.getLogger(__name__)

_INDEX_HTML: Final = Path(__file__).parent / "static" / "index.html"
MAX_TICKET_CHARS: Final = 5000


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_text: str = Field(min_length=5, max_length=MAX_TICKET_CHARS)


def create_app(
    settings: Settings | None = None, classifier: TicketClassifier | None = None
) -> FastAPI:
    """Build the app. Tests pass a classifier backed by a fake LLM; production builds one
    on the Claude subscription backend at startup."""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if classifier is None:
            # Fail at startup, not on the first request, if API credentials are present.
            ensure_subscription_auth()
            llm = ClaudeCodeClient(
                model=resolved_settings.claude_model, effort=resolved_settings.effort
            )
            app.state.classifier = TicketClassifier(llm, resolved_settings)
        else:
            app.state.classifier = classifier
        logger.info(
            "Ticket classifier ready: model=%s prompt=%s",
            resolved_settings.claude_model,
            resolved_settings.prompt_version,
        )
        yield

    app = FastAPI(title="Support Ticket Classifier", version=__version__, lifespan=lifespan)

    def get_classifier(request: Request) -> TicketClassifier:
        found: TicketClassifier = request.app.state.classifier
        return found

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_INDEX_HTML)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/classify")
    async def classify(
        body: ClassifyRequest,
        ticket_classifier: Annotated[TicketClassifier, Depends(get_classifier)],
    ) -> TicketResult:
        try:
            return await ticket_classifier.classify(body.ticket_text)
        except LLMConfigurationError:
            logger.exception("Classifier backend is misconfigured")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Classifier is not configured correctly. See the server logs.",
            ) from None

    return app
