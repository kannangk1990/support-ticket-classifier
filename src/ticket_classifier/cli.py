"""Command-line entry point: ``ticket-classifier {classify,serve,eval}``."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from ticket_classifier.classifier import TicketClassifier
from ticket_classifier.config import Settings, get_settings
from ticket_classifier.evaluation import load_dataset, run_eval, save_report
from ticket_classifier.llm import LLMConfigurationError
from ticket_classifier.llm.claude_code import ClaudeCodeClient

DEFAULT_EVAL_DATASET = Path("evals/tickets.jsonl")
DEFAULT_EVAL_RESULTS = Path("evals/results")


def _build_classifier(settings: Settings) -> TicketClassifier:
    llm = ClaudeCodeClient(model=settings.claude_model, effort=settings.effort)
    return TicketClassifier(llm, settings)


def _classify(settings: Settings, text: str) -> int:
    if text == "-":
        text = sys.stdin.read()
    result = asyncio.run(_build_classifier(settings).classify(text))
    print(result.model_dump_json(indent=2))
    return 0


def _evaluate(
    settings: Settings, dataset: Path, limit: int | None, repeat: int, results_dir: Path
) -> int:
    tickets = load_dataset(dataset)[:limit]
    print(
        f"Running {len(tickets)} tickets x {repeat} on your Claude subscription...",
        file=sys.stderr,
    )
    report = asyncio.run(run_eval(_build_classifier(settings), tickets, settings, repeats=repeat))
    print(report.render())
    print(f"Saved {save_report(report, results_dir)}", file=sys.stderr)
    return 0


def _serve(settings: Settings) -> int:
    import uvicorn  # imported lazily: only the serve command needs it

    from ticket_classifier.api import create_app

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ticket-classifier", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    classify = commands.add_parser("classify", help="classify one ticket and print JSON")
    classify.add_argument("text", help="ticket text, or '-' to read from stdin")

    commands.add_parser("serve", help="run the HTTP API and demo page")

    evaluate = commands.add_parser("eval", help="measure accuracy on a labelled dataset")
    evaluate.add_argument("--dataset", type=Path, default=DEFAULT_EVAL_DATASET)
    evaluate.add_argument("--limit", type=int, default=None, help="only run the first N tickets")
    evaluate.add_argument(
        "--repeat", type=int, default=1, help="run each ticket N times to measure consistency"
    )
    evaluate.add_argument("--results-dir", type=Path, default=DEFAULT_EVAL_RESULTS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        if args.command == "classify":
            return _classify(settings, args.text)
        if args.command == "eval":
            if args.repeat < 1:
                print("error: --repeat must be at least 1", file=sys.stderr)
                return 2
            return _evaluate(settings, args.dataset, args.limit, args.repeat, args.results_dir)
        return _serve(settings)
    except LLMConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
