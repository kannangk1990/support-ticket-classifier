"""HTTP API and demo page."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Final

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field

from ticket_classifier import __version__
from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings, get_settings
from ticket_classifier.llm import LLMConfigurationError
from ticket_classifier.llm.claude_code import ClaudeCodeClient, ensure_subscription_auth
from ticket_classifier.metrics import REGISTRY
from ticket_classifier.models import TicketResult
from ticket_classifier.service import StorageDisabledError, TicketService
from ticket_classifier.storage import TicketStore

logger = logging.getLogger(__name__)

_INDEX_HTML: Final = Path(__file__).parent / "static" / "index.html"
MAX_TICKET_CHARS: Final = 5000


class ClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_text: str = Field(min_length=5, max_length=MAX_TICKET_CHARS)
    # Optional idempotency key: a repeat returns the stored result instead of calling Claude.
    external_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )


def create_app(
    settings: Settings | None = None,
    classifier: TicketClassifier | None = None,
    store: TicketStore | None = None,
) -> FastAPI:
    """Build the app. Tests pass a classifier backed by a fake LLM (and a store only if
    they need one); production builds both at startup."""
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_store = store
        if classifier is None:
            # Fail at startup, not on the first request, if API credentials are present.
            ensure_subscription_auth()
            llm = ClaudeCodeClient(
                model=resolved_settings.claude_model, effort=resolved_settings.effort
            )
            resolved_classifier = TicketClassifier(llm, resolved_settings)
            resolved_store = store or TicketStore(resolved_settings.db_path)
        else:
            resolved_classifier = classifier
        app.state.service = TicketService(resolved_classifier, resolved_store)
        logger.info(
            "Ticket classifier ready: model=%s prompt=%s",
            resolved_settings.claude_model,
            resolved_settings.prompt_version,
        )
        yield

    app = FastAPI(title="Support Ticket Classifier", version=__version__, lifespan=lifespan)

    def get_service(request: Request) -> TicketService:
        found: TicketService = request.app.state.service
        return found

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_INDEX_HTML)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/classify")
    async def classify(
        body: ClassifyRequest,
        service: Annotated[TicketService, Depends(get_service)],
    ) -> TicketResult:
        try:
            return await service.classify(body.ticket_text, body.external_id)
        except LLMConfigurationError:
            logger.exception("Classifier backend is misconfigured")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Classifier is not configured correctly. See the server logs.",
            ) from None

    @app.get("/api/v1/tickets/{ticket_id}")
    async def get_ticket(
        ticket_id: str, service: Annotated[TicketService, Depends(get_service)]
    ) -> TicketResult:
        try:
            found = await service.get(ticket_id)
        except StorageDisabledError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Storage is not enabled."
            ) from None
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found.")
        return found

    return app
