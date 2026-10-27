# 2026-10-27: hardening summary

A one-day pass that took the classifier from a working prototype toward a production
shape: visible redaction, tests as a hard gate, measured injection handling, stronger PII
detection, resilient retries, storage, metrics and alerts, a better prompt, and a retention
command. The reasoning behind each choice is in [decisions.md](decisions.md); this page
records what was done.

**Starting point:** 71 offline tests passed, `main` was unprotected, nothing was stored,
and the eval had 22 tickets.
**End point:** 187 offline tests pass, `main` is protected, and a 35-ticket eval has run
live on both prompt versions.

## Pull requests

All times are UTC, on 2026-10-27.

| PR | Merged | What it did |
|---|---|---|
| #7 | 13:28 | Shows the redacted text in the UI ("Sent to Claude") and API, logs each pipeline step at DEBUG, adds log-safety tests, the pre-push test gate, and the no-test-weakening rule in `CLAUDE.md` |
| #3–#6 | 13:24–13:42 | Dependabot: allows newer `uv-build`; `setup-uv` v7, `checkout` v7, `gitleaks-action` v3 |
| #8 | 13:51 | Groups Dependabot updates into one PR per ecosystem |
| #9 | 14:33 | **A. Measurement:** `eval --repeat N` for consistency, injection and false-positive scores, 10 attack and 5 look-alike tickets, saved JSON reports |
| #10 | 15:23 | **B. Safety:** tightened injection guard, IBAN, SSN and IP detection, PII check on Claude's reasoning, prompt hash pinning, API contract snapshots |
| #11 | 15:35 | **C. Reliability:** backoff with jitter, refusals not retried, retry feedback naming invalid fields, circuit breaker |
| #12 | 15:40 | **D. Storage:** SQLite store, `external_id` idempotency, `GET /api/v1/tickets/{id}`, `reprocess` command |
| #13 | 15:46 | **E. Monitoring:** Prometheus `/metrics`, in-app alerts, `deploy/prometheus/` alert rules |
| #14 | 15:56 | Eval report names priority mistakes and unstable tickets |
| #15 | 16:09 | Eval report names review mistakes with reasons; README results updated |
| #16 | 16:10 | Prompt `v2`: delivery delays of 3 or more days are `high` |
| #17 | 16:18 | `v2` becomes the default; `lookalike-02` relabelled to expect review |
| #18 | 16:27 | Removes the prompt version override from `.env.example` |
| #19 | 16:41 | `purge --older-than-days N`: erases old ticket text and keeps the classification (`--delete` removes whole records) |
| #20 | 16:46 | README: step-by-step guide to running locally, with troubleshooting |

## Repository settings changed

- **Branch protection on `main`:**
  - every change must come through a pull request;
  - CI jobs `checks` and `secrets` must pass;
  - branches must be up to date with `main`;
  - the rules apply to admins too;
  - force-pushes and deletion are blocked.
- **"Automatically delete head branches"** is turned on.
- **Dependabot** runs weekly with grouped updates, configured in `.github/dependabot.yml`.

## Eval results (live, `claude-opus-5-5`, effort `low`, 35 tickets × 2 runs)

| Metric | Prompt `v1` | Prompt `v2` |
|---|---|---|
| Category accuracy | 54/54 | 54/54 |
| Priority accuracy | 52/54 | **54/54** |
| Human-review flag | 68/70 | 68/70 |
| Injection attacks caught | 20/20 | 20/20 |
| Legitimate tickets not flagged | 50/50 | 50/50 |
| Same answer on both runs | 35/35 | 35/35 |
| Fallbacks | 0 | 0 |

- **`v1` priority misses:** `delivery-01` both times. This led to `v2`.
- **Review misses (both prompts):** `lookalike-02`, which was then relabelled. The next run
  should show 70/70, but it **hasn't been re-run yet**.
- **Live check after switching to `v2`:** the "tracking stuck for 6 days" ticket returned
  `priority=high`, with no review and on the first attempt.

## Decisions made along the way

These are the owner's choices; details and alternatives are in [decisions.md](decisions.md).

- Authentication and rate limiting are out of scope: the app runs on localhost for one user (D4).
- API cost projection is out of scope (D1).
- Prometheus for metrics, regex-only PII, storing the redacted text, and one PR at a time
  with the owner merging (D5, D12, D13).
- The guard was tightened, and one PR A test setup was changed with approval (D3, D7).
- A delivery delay of 3 or more days is `high` priority (D8).
- `lookalike-02` should go to human review (D15).
- Purge defaults to erasing text (option B) (D14).

## Still open

- **Names, postal addresses and dates of birth aren't detected.** They reach Claude and
  storage until purged (D5, D14).
- **`purge` isn't scheduled.** Run it by hand or from cron. Database backups aren't purged.
- **The Prometheus rules haven't been checked with `promtool`.**
- **No live eval since `lookalike-02` was relabelled.** 70/70 is expected for review.
- **The eval set is small and hand-written.** Add real, anonymised tickets before relying
  on the numbers.
- **The test suite takes about 3 s** because older retry tests now use the real backoff (D10).
- **Claude Code can't read `.env.example`** under the local permission settings, so changes
  to it are made by the owner.
