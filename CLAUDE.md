# Project notes for Claude Code

- Claude is called **only** through the Claude Agent SDK on the user's subscription
  (`src/ticket_classifier/llm/claude_code.py`). Never add the `anthropic` SDK, an API-key
  path, or anything that reads `ANTHROPIC_API_KEY`, unless the user explicitly asks.
- Ticket text must be redacted (`pii.redact_pii`) before it reaches any LLM call or log line.
  Log ticket IDs and PII types, never ticket content.
- Team routing and the human-review decision belong in `routing.py`, not in the prompt.
- Don't edit a released prompt version; add a new one in `prompts.py` and compare them with `ticket-classifier eval`.
- Tests must stay offline: use `tests/fakes.FakeLLM`. Real calls go only in tests marked `live`.
- Before finishing a change, run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`.
- Every change needs tests for its new behaviour, and the full suite must pass before pushing
  (the pre-push hook enforces this). Never modify, weaken, skip or delete an existing test to
  make a change pass; fix the code instead, and if a test itself looks wrong, ask the user first.
