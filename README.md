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
| Privacy | Emails, phone numbers, Luhn-valid card numbers, checksum-valid IBANs, US SSNs and IP addresses are replaced with placeholders **before** anything is sent to Claude. Logs record ticket IDs and PII *types*, never ticket text. If Claude's reasoning still contains personal data, it is redacted and the ticket goes to human review. |
| Prompt injection | Ticket text is passed as delimited data. A cheap regex pre-check stops blatant attacks before they cost a call. Claude reports `injection_suspected` for subtler ones, which forces human review. |
| Consistent routing | Claude picks only the category, priority and sentiment. The team and the human-review decision come from fixed rules in [`routing.py`](src/ticket_classifier/routing.py), so they can't contradict each other. |
| Reliability | Output is validated with Pydantic. Invalid output is retried with a note naming the bad fields (never their values); outages are retried after a random exponential backoff; refusals aren't retried. Failures end in a safe fallback that goes to a human. A circuit breaker skips Claude for a cool-down after repeated outages instead of making every ticket wait for timeouts. A misconfigured backend fails loudly instead of being masked. |
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

## Run it locally, step by step

**1. Install the tools (once).**

- [uv](https://docs.astral.sh/uv/getting-started/installation/), e.g. `brew install uv` on macOS.
  You don't need to install Python yourself: `uv` downloads Python 3.12 automatically
  (the version pinned in `.python-version`).
- [Claude Code](https://claude.com/claude-code). Run `claude`, then `/login`, and sign in
  with your Pro or Max subscription.

**2. Get the code and install dependencies.**

```bash
git clone https://github.com/kannangk1990/support-ticket-classifier.git
cd support-ticket-classifier
uv sync
```

**3. Make sure no API key is set.** The app refuses to start if one is, so it can never bill
the API by accident. This prints only the *names* of any such variables, never their values:

```bash
env | cut -d= -f1 | grep -E '^(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|CLAUDE_CODE_USE_)'
```

No output means you're fine. Otherwise remove each listed variable (e.g.
`unset ANTHROPIC_API_KEY`) and from your shell profile too.

**4. Optional: check the install offline.** The tests use a fake Claude, so this uses none
of your subscription:

```bash
uv run pytest
```

**5. Start the server with step-by-step logs.**

```bash
TICKET_LOG_LEVEL=DEBUG uv run ticket-classifier serve
```

Wait for this startup line:

```text
INFO ticket_classifier.api: Ticket classifier ready: model=claude-opus-5-5 prompt=v2
```

**6. Classify a ticket in the browser.** Open <http://127.0.0.1:8000> and paste:

```text
Tracking has said 'out for delivery' for 6 days. Email me at jane.doe@example.com
```

After a few seconds the page shows the result: category `delivery issue`, team
`logistics team` and priority `high`. *PII redacted* shows `email`, and *Sent to Claude*
shows the text with `[EMAIL]` in place of the address. The terminal logs each step for
that ticket, without the ticket text:

```text
ticket=… received chars=… redacted pii=email
ticket=… guard passed
ticket=… attempt 1 started
ticket=… attempt 1 succeeded duration_ms=… tokens_in=… tokens_out=…
ticket=… category=delivery_issue priority=high review=False attempts=1 pii=email
```

**7. Stop the server** with Ctrl+C. Results are kept in `data/tickets.db` (gitignored);
see the `purge` command in the Quick start below.

> **Usage:** every classification starts a Claude Code session on your subscription and
> counts toward its limits. A full `eval --repeat 2` makes 70 calls; use `--limit` for a
> cheaper run.

**If something goes wrong**

| Symptom | Fix |
|---|---|
| `Refusing to call Claude: ANTHROPIC_API_KEY is set …` | Step 3: unset the variable. |
| `Claude Code CLI not found` | Run `uv sync` again. |
| Every ticket falls back to human review | You're probably not signed in. Run `claude`, then `/login`. |
| `address already in use` | Another server is on port 8000. Stop it, or start with `TICKET_PORT=8001`. |
| Startup line shows `prompt=v1` | A `.env` file sets `TICKET_PROMPT_VERSION`. Remove that line. |

## Quick start

```bash
uv sync                                   # create .venv and install locked dependencies
uv run ticket-classifier classify "I was charged twice for order #4512"
uv run ticket-classifier serve            # http://127.0.0.1:8000 (demo page + API)
uv run ticket-classifier eval             # accuracy on evals/tickets.jsonl
uv run ticket-classifier reprocess        # re-run stored tickets that fell back
uv run ticket-classifier purge --older-than-days 90   # erase text of old stored tickets
```

Read from stdin with `classify -`. To see the API schema, run the server and open
`http://127.0.0.1:8000/docs`.

`eval` reports category, priority and review accuracy, how many injection attacks were
caught, and how many legitimate tickets were wrongly flagged as injections. Add
`--repeat 3` to run each ticket three times and see how stable the answers are. The report
names every category, priority and review-flag mistake by ticket and run (with the review
reasons), and lists tickets whose answers changed between runs with the values seen. Each
run is saved as JSON in `evals/results/` (gitignored) so you can compare runs over time.

### API

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/classify \
  -H 'content-type: application/json' \
  -d '{"ticket_text": "My parcel has said out for delivery for 6 days"}'
```

Pass an optional `external_id` (your helpdesk's ticket ID: letters, digits, `.`, `_`, `:`,
`-`; up to 128 characters) to make the call idempotent. A repeat returns the stored result
without calling Claude, unless the stored result was a fallback, in which case the ticket
is classified again under the same `ticket_id`. Fetch a stored result with
`GET /api/v1/tickets/{ticket_id}`.

The response includes:
- `status`: one of `classified`, `flagged_injection` or `fallback`
- the classification fields
- `review_reasons`
- `pii_redacted`: the types of personal data removed
- `redacted_text`: the ticket exactly as Claude received it, with placeholders such as
  `[EMAIL]`. The demo page shows it as "Sent to Claude". It is never logged.
- `usage`: tokens, duration, and the API-equivalent cost. You are not billed this cost
  on a subscription; it shows how much of your plan the call used.

## Monitoring

`GET /metrics` serves Prometheus metrics. Every label comes from a fixed list, so metrics
never contain ticket text:

| Metric | What it counts |
|---|---|
| `ticket_classifier_tickets_total{status}` | Tickets by final status |
| `ticket_classifier_categories_total{category,priority}` | Classified categories and priorities |
| `ticket_classifier_review_reasons_total{reason}` | Why tickets went to human review |
| `ticket_classifier_pii_redacted_total{type}` | Tickets with each type of personal data |
| `ticket_classifier_llm_attempts_total{outcome}` | Claude calls: success, unavailable, invalid_output, refused |
| `ticket_classifier_llm_tokens_total{direction}` | Input and output tokens |
| `ticket_classifier_duration_seconds` | End-to-end time per ticket (histogram) |
| `ticket_classifier_circuit_state` | 0 closed, 1 half-open, 2 open |
| `ticket_classifier_storage_errors_total`, `ticket_classifier_stored_result_hits_total` | Storage failures, and repeats answered from storage |
| `ticket_classifier_alert_firing{alert}` | 1 while an in-app alert is firing |

**Alerts** work two ways:

- **In the app, with no setup:** an `ALERT FIRING` line is logged at ERROR when the circuit
  opens, or when the fallback or injection rate over the last `TICKET_ALERT_WINDOW_TICKETS`
  tickets crosses its threshold. An `ALERT RESOLVED` line is logged at INFO when it clears.
  Each alert logs once when it starts and once when it clears, not on every ticket.
- **With Prometheus:** [`deploy/prometheus/alerts.yml`](deploy/prometheus/alerts.yml) has the
  same rules plus service-down, slow-ticket and storage-error alerts. Route them with Alertmanager.
  [`prometheus.example.yml`](deploy/prometheus/prometheus.example.yml) scrapes a local server.

## Configuration

Settings come from environment variables or a `.env` file. Copy [`.env.example`](.env.example) to start.

| Variable | Default | Meaning |
|---|---|---|
| `TICKET_CLAUDE_MODEL` | `claude-opus-5-5` | Model Claude Code uses |
| `TICKET_EFFORT` | `low` | Reasoning effort (`low` … `max`) |
| `TICKET_PROMPT_VERSION` | `v2` | Prompt version from [`prompts.py`](src/ticket_classifier/prompts.py) |
| `TICKET_LLM_TIMEOUT_SECONDS` | `120` | Per-attempt timeout |
| `TICKET_MAX_ATTEMPTS` | `2` | Attempts before the safe fallback |
| `TICKET_MAX_CONCURRENCY` | `2` | Parallel Claude sessions (subscriptions are rate limited) |
| `TICKET_REVIEW_CONFIDENCE_THRESHOLD` | `0.7` | Below this, a human reviews the ticket |
| `TICKET_RETRY_BASE_DELAY_SECONDS` / `TICKET_RETRY_MAX_DELAY_SECONDS` | `1` / `10` | Random backoff before retrying a transient failure, doubling per attempt up to the cap |
| `TICKET_CIRCUIT_FAILURE_THRESHOLD` | `5` | Tickets in a row that can't reach Claude before the circuit opens |
| `TICKET_CIRCUIT_RESET_SECONDS` | `30` | How long an open circuit sends tickets straight to human review before trying Claude again |
| `TICKET_ALERT_WINDOW_TICKETS` | `20` | Recent tickets the in-app alerts look at |
| `TICKET_ALERT_FALLBACK_RATE` / `TICKET_ALERT_INJECTION_RATE` | `0.2` / `0.3` | In-app alert thresholds |
| `TICKET_DB_PATH` | `data/tickets.db` | SQLite file for results and redacted text (gitignored) |
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
  service.py         idempotent classify, lookup and reprocess on top of storage
  storage.py         SQLite result store (redacted text only)
  resilience.py      circuit breaker
  metrics.py         Prometheus metrics
  alerts.py          in-app alerts over recent tickets
  api.py, cli.py     FastAPI app and CLI
  evaluation.py      accuracy measurement
evals/tickets.jsonl  labelled tickets
deploy/prometheus/   alert rules and an example scrape config
tests/               offline unit tests plus one opt-in live test
```

### Changing the prompt

Add a new version to `PROMPTS` in `prompts.py` rather than editing a released one.
`tests/test_contracts.py` stores a hash of each released prompt and fails if one is edited,
so also add the new version's hash there. The same file checks that the API stays
backward compatible with the snapshots in `tests/contracts/`.
Then compare the versions:

```bash
TICKET_PROMPT_VERSION=v1 uv run ticket-classifier eval
TICKET_PROMPT_VERSION=v2 uv run ticket-classifier eval
```

## Results

On 2026-10-27 the eval ran on `evals/tickets.jsonl` with `claude-opus-5-5` at effort `low`,
each of the 35 tickets run twice (`eval --repeat 2`, 70 runs). The set has 20 normal
tickets, 10 injection attacks and 5 legitimate tickets that resemble attacks. Attack
tickets have no category or priority label, so those scores are out of 54 runs.

| Metric | Prompt `v1` | Prompt `v2` (default) |
|---|---|---|
| Category accuracy | 54/54 (100%) | 54/54 (100%) |
| Priority accuracy | 52/54 (96%) | **54/54 (100%)** |
| Human-review flag | 68/70 (97%) | 68/70 (97%) |
| Injection attacks caught | 20/20 (100%) | 20/20 (100%) |
| Legitimate tickets not flagged as attacks | 50/50 (100%) | 50/50 (100%) |
| Same answer on both runs (category, priority, review) | 35/35 tickets | 35/35 tickets |
| Fallbacks | 0 | 0 |

- **Priority:** `v1` rated `delivery-01` (tracking stuck for 6 days, labelled `high`) as
  `medium`. `v2` adds one rule, that a parcel 3 or more days late or with tracking unchanged
  for 3 or more days is `high`, and fixes it with no other change in results.
- **Review:** both misses were `lookalike-02` ("setup instructions … missing from the box"),
  which fits both `order_issue` (missing items) and `product_issue`. Claude correctly gave it
  low confidence, so it went to human review. That is the intended outcome, so its label was
  corrected to `expect_review: true` after these runs. Expect 70/70 on the next run.
- **Injections:** the regex guard alone blocks 6 of the 10 attacks. Claude's
  `injection_suspected` flag caught the other 4 on every run.

The set is small and hand-written. Add real (anonymised) tickets before relying on these numbers.

## Limitations

- Stored redacted text can still contain personal data the regexes miss, such as names.
  Run `purge --older-than-days N` regularly (e.g. from cron) to erase the ticket text and
  Claude's reasoning from older records. Category, team, priority, review reasons and
  timings are kept. Add `--delete` to remove whole records. Purged tickets can't be
  reprocessed. The erased bytes are overwritten in the file (SQLite `secure_delete`), but
  copies in backups are not, so treat backups of `data/tickets.db` as sensitive too.

- PII redaction is regex-based. It doesn't catch names, postal addresses, dates of birth
  or non-IBAN account numbers in free text.
- The injection pre-check only catches blatant patterns. Subtle attempts depend on
  Claude's `injection_suspected` flag and the fact that the output can only be a classification.
- Each call starts a Claude Code session, which takes a few seconds and counts against
  your plan's usage limits.

## License

[MIT](LICENSE)
