"""Measure classification accuracy against a labelled set of tickets."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.models import ClassificationStatus, IssueCategory, Priority, TicketResult


class LabelledTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    category: IssueCategory
    priority: Priority
    expect_review: bool = False


@dataclass
class EvalReport:
    total: int = 0
    category_correct: int = 0
    priority_correct: int = 0
    review_correct: int = 0
    fallbacks: int = 0
    mistakes: list[str] = field(default_factory=list)

    def record(self, ticket: LabelledTicket, result: TicketResult) -> None:
        self.total += 1
        if result.status is ClassificationStatus.FALLBACK:
            self.fallbacks += 1
        if result.issue_category is ticket.category:
            self.category_correct += 1
        else:
            self.mistakes.append(
                f"{ticket.id}: expected {ticket.category}, got {result.issue_category}"
            )
        if result.priority is ticket.priority:
            self.priority_correct += 1
        if result.requires_human_review == ticket.expect_review:
            self.review_correct += 1

    def render(self) -> str:
        def pct(correct: int) -> str:
            return f"{correct}/{self.total} ({correct / self.total:.0%})" if self.total else "n/a"

        lines = [
            f"Tickets evaluated : {self.total}",
            f"Category accuracy : {pct(self.category_correct)}",
            f"Priority accuracy : {pct(self.priority_correct)}",
            f"Review flag match : {pct(self.review_correct)}",
            f"Fallbacks         : {self.fallbacks}",
        ]
        if self.mistakes:
            lines.append("Category mistakes:")
            lines.extend(f"  - {mistake}" for mistake in self.mistakes)
        return "\n".join(lines)


def load_dataset(path: Path) -> list[LabelledTicket]:
    tickets: list[LabelledTicket] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                tickets.append(LabelledTicket.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid eval record: {exc}") from exc
    return tickets


async def run_eval(classifier: TicketClassifier, tickets: Sequence[LabelledTicket]) -> EvalReport:
    # The classifier's semaphore caps concurrency, so gathering everything is safe.
    results = await asyncio.gather(*(classifier.classify(ticket.text) for ticket in tickets))
    report = EvalReport()
    for ticket, result in zip(tickets, results, strict=True):
        report.record(ticket, result)
    return report
