# Support Ticket Classifier

Classifies customer support tickets with Claude and routes them to the right team.
For each ticket it returns:

- the issue category
- the owning team
- the priority
- the customer's sentiment
- a confidence score
- whether a human needs to review it

It runs **locally on your Claude subscription** (Pro or Max) through Claude Code. No
API key is needed, and the app refuses to run if one is present.

```text
ticket ──▶ redact PII ──▶ injection pre-check ──▶ Claude (structured output) ──▶ routing rules ──▶ result
              │                  │ flagged                    │ fails twice
              ▼                  ▼                            ▼
     emails, phones, cards    human review               safe fallback → human review
     never leave the machine  (no Claude call)
```

## Why it is built this way

| Concern | Approach |
|---|---|
| Privacy | Emails, phone numbers and Luhn-valid card numbers are replaced with placeholders **before** anything is sent to Claude. Logs record ticket IDs and PII *types*, never ticket text. |
| Prompt injection | Ticket text is passed as delimited data. A cheap regex pre-check stops blatant attacks before they cost a call. Claude reports `injection_suspected` for subtler ones, which forces human review. |
| Consistent routing | Claude picks only the category, priority and sentiment. The team and the human-review decision come from fixed rules in [`routing.py`](src/ticket_classifier/routing.py), so they can't contradict each other. |
| Reliability | Output is validated with Pydantic. A bad or missing result is retried once, then replaced by a safe fallback that goes to a human. A misconfigured backend fails loudly instead of being masked. |
| Billing safety | The backend refuses to start if `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or a cloud-provider switch is set. It also checks that Claude Code reports subscription auth (`apiKeySource: none`) before each call. |
| No tool access | Claude runs with no tools and none of your local Claude Code settings, so a ticket can't make it read files or run commands. |
| Measurable | `evals/tickets.jsonl` holds labelled tickets, and `ticket-classifier eval` reports accuracy for them. |

## Requirements

- macOS or Linux, with [uv](https://docs.astral.sh/uv/) installed
- A Claude Pro or Max subscription, signed in to Claude Code: run `claude` and then `/login`.
  The Claude Agent SDK ships its own Claude Code binary, and that binary uses the same login.

> **Personal use only.** Anthropic does not allow products to offer claude.ai login to
> other people. Run this for yourself, on your own machine and subscription. Don't host
> it as a service for others.

## Quick start

```bash
uv sync                                   # create .venv and install locked dependencies
uv run ticket-classifier classify "I was charged twice for order #4512"
uv run ticket-classifier serve            # http://127.0.0.1:8000 (demo page + API)
uv run ticket-classifier eval             # accuracy on evals/tickets.jsonl
```

Read from stdin with `classify -`. To see the API schema, run the server and open
`http://127.0.0.1:8000/docs`.

`eval` reports category, priority and review accuracy, how many injection attacks were
caught, and how many legitimate tickets were wrongly flagged as injections. Add
`--repeat 3` to run each ticket three times and see how stable the answers are. Each run is
saved as JSON in `evals/results/` (gitignored) so you can compare runs over time.

### API

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/classify \
  -H 'content-type: application/json' \
  -d '{"ticket_text": "My parcel has said out for delivery for 6 days"}'
```

The response includes:
- `status`: one of `classified`, `flagged_injection` or `fallback`
- the classification fields
- `review_reasons`
- `pii_redacted`: the types of personal data removed
- `redacted_text`: the ticket exactly as Claude received it, with placeholders such as
  `[EMAIL]`. The demo page shows it as "Sent to Claude". It is never logged.
- `usage`: tokens, duration, and the API-equivalent cost. You are not billed this cost
  on a subscription; it shows how much of your plan the call used.

## Configuration

Settings come from environment variables or a `.env` file. Copy [`.env.example`](.env.example) to start.

| Variable | Default | Meaning |
|---|---|---|
| `TICKET_CLAUDE_MODEL` | `claude-opus-5-5` | Model Claude Code uses |
| `TICKET_EFFORT` | `low` | Reasoning effort (`low` … `max`) |
| `TICKET_PROMPT_VERSION` | `v1` | Prompt version from [`prompts.py`](src/ticket_classifier/prompts.py) |
| `TICKET_LLM_TIMEOUT_SECONDS` | `120` | Per-attempt timeout |
| `TICKET_MAX_ATTEMPTS` | `2` | Attempts before the safe fallback |
| `TICKET_MAX_CONCURRENCY` | `2` | Parallel Claude sessions (subscriptions are rate limited) |
| `TICKET_REVIEW_CONFIDENCE_THRESHOLD` | `0.7` | Below this, a human reviews the ticket |
| `TICKET_HOST` / `TICKET_PORT` | `127.0.0.1` / `8000` | Server bind address |

## Development

```bash
uv sync
uv run pre-commit install   # commit: ruff, mypy, gitleaks; push: full CI gate incl. pytest
uv run pytest               # offline: Claude is replaced by a fake
uv run pytest -m live       # one real call on your subscription
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

CI runs the same checks on every push and pull request, plus a gitleaks secret scan.

A push is refused unless the whole suite passes. New behaviour needs new tests; existing
tests are never changed just to make a change pass.

### Watching the pipeline

Run the server with `TICKET_LOG_LEVEL=DEBUG` to log each step of every ticket: redaction
(PII types only), the guard check, each Claude attempt with its duration and tokens, and the
final routing. Logs contain ticket IDs, never ticket text; `tests/test_classifier.py` checks this.

### Project layout

```text
src/ticket_classifier/
  classifier.py      pipeline: redact → guard → classify with retry → rules
  llm/claude_code.py Claude Agent SDK backend (subscription only)
  llm/base.py        backend protocol and error types
  pii.py             PII redaction
  guard.py           injection pre-check
  prompts.py         versioned system prompts
  routing.py         category → team map and review rules
  models.py          enums and Pydantic models
  api.py, cli.py     FastAPI app and CLI
  evaluation.py      accuracy measurement
evals/tickets.jsonl  labelled tickets
tests/               offline unit tests plus one opt-in live test
```

### Changing the prompt

Add a new version to `PROMPTS` in `prompts.py` rather than editing a released one.
Then compare the versions:

```bash
TICKET_PROMPT_VERSION=v1 uv run ticket-classifier eval
TICKET_PROMPT_VERSION=v2 uv run ticket-classifier eval
```

## Results

On 2026-10-26 the eval ran on `evals/tickets.jsonl` (22 tickets) with `claude-opus-5-5` at effort `low`:

| Metric | Score |
|---|---|
| Category accuracy | 22/22 |
| Priority accuracy | 21/22 |
| Human-review flag | 22/22 |
| Fallbacks | 0 |

The set is small and hand-written. Add real (anonymised) tickets before relying on these numbers.

## Limitations

- PII redaction is regex-based. It doesn't catch names, postal addresses or account
  numbers in free text.
- The injection pre-check only catches blatant patterns. Subtle attempts depend on
  Claude's `injection_suspected` flag and the fact that the output can only be a classification.
- Each call starts a Claude Code session, which takes a few seconds and counts against
  your plan's usage limits.

## License

[MIT](LICENSE)
