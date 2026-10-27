"""Measure classification accuracy, run-to-run consistency and injection handling against a
labelled set of tickets."""

import asyncio
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings
from ticket_classifier.models import (
    ClassificationStatus,
    IssueCategory,
    Priority,
    TicketResult,
    injection_detected,
)


class LabelledTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    text: str
    # Optional so attack tickets, which have no meaningful category, don't skew accuracy.
    category: IssueCategory | None = None
    priority: Priority | None = None
    expect_review: bool = False
    injection: bool = False


@dataclass
class Score:
    correct: int = 0
    total: int = 0

    def add(self, ok: bool) -> None:
        self.total += 1
        self.correct += int(ok)

    def render(self) -> str:
        if not self.total:
            return "n/a"
        return f"{self.correct}/{self.total} ({self.correct / self.total:.0%})"


@dataclass
class EvalReport:
    model: str
    prompt_version: str
    effort: str
    repeats: int
    tickets: int = 0
    runs: int = 0
    fallbacks: int = 0
    category: Score = field(default_factory=Score)
    priority: Score = field(default_factory=Score)
    review: Score = field(default_factory=Score)
    injection_caught: Score = field(default_factory=Score)
    # Correct here means a legitimate ticket was NOT flagged as an injection.
    legitimate_not_flagged: Score = field(default_factory=Score)
    # A ticket is stable when every repeat gave the same answer.
    stable_category: Score = field(default_factory=Score)
    stable_priority: Score = field(default_factory=Score)
    stable_review: Score = field(default_factory=Score)
    # Category mistakes; kept under this name so older saved reports stay comparable.
    mistakes: list[str] = field(default_factory=list)
    priority_mistakes: list[str] = field(default_factory=list)
    # Tickets whose answers differed between repeats, with the values seen, in run order.
    unstable: list[str] = field(default_factory=list)

    def record(self, ticket: LabelledTicket, results: Sequence[TicketResult]) -> None:
        self.tickets += 1
        for run, result in enumerate(results, start=1):
            self.runs += 1
            if result.status is ClassificationStatus.FALLBACK:
                self.fallbacks += 1
            if ticket.category is not None:
                self.category.add(result.issue_category is ticket.category)
                if result.issue_category is not ticket.category:
                    self.mistakes.append(
                        f"{ticket.id} (run {run}): expected {ticket.category}, "
                        f"got {result.issue_category}"
                    )
            if ticket.priority is not None:
                self.priority.add(result.priority is ticket.priority)
                if result.priority is not ticket.priority:
                    self.priority_mistakes.append(
                        f"{ticket.id} (run {run}): expected {ticket.priority}, "
                        f"got {result.priority}"
                    )
            self.review.add(result.requires_human_review == ticket.expect_review)
            if ticket.injection:
                self.injection_caught.add(injection_detected(result))
            else:
                self.legitimate_not_flagged.add(not injection_detected(result))

        if len(results) > 1:
            self.stable_category.add(len({r.issue_category for r in results}) == 1)
            self.stable_priority.add(len({r.priority for r in results}) == 1)
            self.stable_review.add(len({r.requires_human_review for r in results}) == 1)
            differences = [
                f"{name} {'/'.join(values)}"
                for name, values in (
                    ("category", _distinct(str(r.issue_category) for r in results)),
                    ("priority", _distinct(str(r.priority) for r in results)),
                    (
                        "review",
                        _distinct("yes" if r.requires_human_review else "no" for r in results),
                    ),
                )
                if len(values) > 1
            ]
            if differences:
                self.unstable.append(f"{ticket.id}: {'; '.join(differences)}")

    def render(self) -> str:
        lines = [
            f"Model / prompt     : {self.model} / {self.prompt_version} (effort {self.effort})",
            f"Tickets evaluated  : {self.tickets} x {self.repeats} run(s) = {self.runs}",
            f"Category accuracy  : {self.category.render()}",
            f"Priority accuracy  : {self.priority.render()}",
            f"Review flag match  : {self.review.render()}",
            f"Injections caught  : {self.injection_caught.render()}",
            f"Legit not flagged  : {self.legitimate_not_flagged.render()}",
            f"Fallbacks          : {self.fallbacks}",
        ]
        if self.repeats > 1:
            lines += [
                f"Stable category    : {self.stable_category.render()}",
                f"Stable priority    : {self.stable_priority.render()}",
                f"Stable review flag : {self.stable_review.render()}",
            ]
        for title, entries in (
            ("Category mistakes:", self.mistakes),
            ("Priority mistakes:", self.priority_mistakes),
            ("Unstable tickets:", self.unstable),
        ):
            if entries:
                lines.append(title)
                lines.extend(f"  - {entry}" for entry in entries)
        return "\n".join(lines)


def _distinct(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))  # keeps first-seen order


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


async def run_eval(
    classifier: TicketClassifier,
    tickets: Sequence[LabelledTicket],
    settings: Settings,
    *,
    repeats: int = 1,
) -> EvalReport:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    # The classifier's semaphore caps concurrency, so gathering everything is safe.
    results = await asyncio.gather(
        *(classifier.classify(ticket.text) for ticket in tickets for _ in range(repeats))
    )
    report = EvalReport(
        model=settings.claude_model,
        prompt_version=settings.prompt_version,
        effort=settings.effort,
        repeats=repeats,
    )
    for index, ticket in enumerate(tickets):
        report.record(ticket, results[index * repeats : (index + 1) * repeats])
    return report


def save_report(report: EvalReport, directory: Path, *, now: datetime | None = None) -> Path:
    """Write the report as JSON so runs can be compared over time. It holds ticket IDs and
    labels only, never ticket text."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}-{report.prompt_version}.json"
    path.write_text(json.dumps({"created": stamp, **asdict(report)}, indent=2) + "\n")
    return path
