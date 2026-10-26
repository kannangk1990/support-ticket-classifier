"""End-to-end check against real Claude on your subscription. Skipped by default because
it uses your plan's usage; run with ``uv run pytest -m live``."""

import pytest

from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.llm.claude_code import ClaudeCodeClient
from ticket_classifier.models import ClassificationStatus, IssueCategory

pytestmark = pytest.mark.live


async def test_classifies_a_real_ticket() -> None:
    settings = Settings(_env_file=None)
    llm = ClaudeCodeClient(model=settings.claude_model, effort=settings.effort)

    result = await TicketClassifier(llm, settings).classify(
        "I was charged twice for order #4512, please sort it out. Reach me at a@b.io."
    )

    assert result.status is ClassificationStatus.CLASSIFIED
    assert result.issue_category is IssueCategory.PAYMENT
