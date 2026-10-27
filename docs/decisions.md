# Design decisions

Why the classifier is built the way it is. Each entry records what we chose, what we
considered instead, and why. Read this before changing an area, and add an entry when you
make a decision that someone might later want to undo.

What happened on which day is in the dated summaries in this folder, for example
[2026-10-27-hardening-summary.md](2026-10-27-hardening-summary.md).

---

## D1. Claude runs only on the Claude subscription, never the API

**Decision:** Claude is called only through the Claude Agent SDK, signed in with a Claude
Pro or Max subscription. The app refuses to start if `ANTHROPIC_API_KEY`,
`ANTHROPIC_AUTH_TOKEN` or a cloud-provider switch is set, and checks before every call that
Claude Code reports `apiKeySource: none`.

**Why:** this rules out accidental API billing. It also limits the app to **personal use**:
a subscription can't back a service for other people.

**Consequence:** a production deployment for other users would need a second backend on
the Claude API. The `StructuredLLM` protocol in `llm/base.py` allows that, but `CLAUDE.md`
forbids it unless the owner explicitly asks. API cost projection was considered and left
out of scope for the same reason.

## D2. Claude judges meaning; Python makes the decisions that matter

**Decision:** Claude returns only the category, priority, sentiment, confidence, a
one-sentence reasoning and an `injection_suspected` flag. Python decides the team (a fixed
category-to-team map), whether a human reviews the ticket, PII redaction, the injection
pre-check and every fallback. All of these live in `routing.py`, `pii.py`, `guard.py` and
`classifier.py`.

**Why:** the same category always goes to the same team, and routing rules can change
without touching the prompt.

**Known limit:** `confidence` is Claude's own estimate, not a calibrated probability.
The 0.7 review threshold is only as good as that estimate.

## D3. Tests gate every change, and existing tests are not edited to make code pass

**Decision:** every change follows the same path: design, implement, add new tests, and
pass the full suite before pushing. **Existing tests are never modified, weakened, skipped
or deleted to make a change pass.** If a test looks wrong, the owner decides.

**How it's enforced:**
- **Pre-push hook:** `.pre-commit-config.yaml` runs ruff, the format check, mypy and pytest
  before every push.
- **Branch protection on `main`:**
  - every change must come through a pull request;
  - the `checks` and `secrets` CI jobs must pass;
  - the branch must be up to date with `main`;
  - the rules apply to admins too;
  - force-pushes and deleting `main` are blocked;
  - no approvals are required, because a sole owner can't approve their own PR.
- **The rule is in `CLAUDE.md`**, so Claude Code sessions follow it.

**Approved exceptions so far,** each called out in its PR:
- #10: an eval test that relied on a guard bug now gets its false positive from the fake
  LLM instead. Its assertions are unchanged.
- #17: the test guarding the default prompt was updated when the default became `v2`.
- #16: adding a released prompt's hash to `tests/test_contracts.py` is the documented
  release step, not an edit to make a test pass.

## D4. No authentication or rate limiting in the app

**Decision:** neither is built in.

**Why:**
- The server binds to `127.0.0.1`, so nothing outside the machine can reach it.
- The subscription terms mean there are no other users (D1).
- Calls to Claude are already capped by `TICKET_MAX_CONCURRENCY`.

**If it's ever deployed for others:** add the API backend first, then put it behind the
organisation's SSO proxy (for example oauth2-proxy or an API gateway) rather than building
OAuth into the app.

## D5. PII detection is regex-only, with validation

**Decision:** emails; phone numbers; card numbers (Luhn check); IBANs (ISO 13616 mod-97
checksum); US SSNs (dashed form, valid ranges only); IPv4 and IPv6 addresses (validated with
`ipaddress`).

**Rejected:** NER (Presidio and spaCy) to catch names and addresses. It means heavy
dependencies (a spaCy model is hundreds of MB), slower startup, and false positives on
product and place names.

**Consequence:** names, postal addresses and dates of birth reach Claude and storage.
D14 mitigates this.

**Output check:** if Claude's `reasoning` contains regex-detectable PII, it is redacted and
the ticket goes to human review (`pii_in_output`).

## D6. Ticket text never appears in logs or metrics

**Decision:**
- Logs carry ticket IDs, PII *types*, sizes and timings only.
- The redacted text goes back to the caller as `redacted_text` (shown in the UI as "Sent to
  Claude") and into storage, but is never logged.
- Metric labels come only from fixed enums.
- With `TICKET_LOG_LEVEL=DEBUG`, every pipeline step is logged.

**Enforced by:** tests that classify PII-heavy tickets at DEBUG level and assert no
sensitive text appears in any log record. Both the classified and fallback paths are
covered, and the test was shown to fail when a leak is injected. Another test checks the
exported metrics.

## D7. Injection defence is layered and measured

**Decision:** three layers:
1. A regex guard (`guard.py`) that blocks blatant attacks before any Claude call.
2. The ticket is passed as delimited data, with `<` and `>` escaped.
3. Claude's `injection_suspected` flag forces human review.

**The guard was tightened (#10):** "ignore / disregard / forget / override" must target the
assistant's own instructions ("your", "all", "previous", "the above", "system"), and
"act as" counts only when it's addressed to the assistant.

**Why:** the old guard wrongly flagged 3 of 5 legitimate tickets, such as "please ignore
*my* previous instructions about the delivery slot".

**Measured (2026-10-27, live):**
- The guard alone blocks 6 of 10 attacks.
- With Claude's flag, all 10 are caught on every run (20/20).
- No legitimate tickets were flagged (50/50 not flagged).

## D8. Prompts are versioned, frozen and compared before switching

**Decision:**
- A released prompt is never edited. `tests/test_contracts.py` pins each one by SHA-256.
- A new version is a copy with the smallest possible change. `tests/test_prompts.py` checks
  that `v2` differs from `v1` only in the intended rule.
- The default switches only after `eval` shows the new version is no worse.

**`v2` (#16, default since #17):** a parcel that is 3 or more days late, or whose tracking
hasn't changed for 3 or more days, is `high` priority. This is a business decision by the
owner. `v1` rated `delivery-01` (tracking stuck for 6 days) as `medium` in 3 of 4 runs.
`v2` scored priority 54/54, against 52/54 for `v1`, and nothing else changed.

## D9. The HTTP API only changes in backward-compatible ways

**Decision:** `tests/contracts/` holds schema snapshots of the request and the response.
The test passes when an optional field is added, and fails when a field or allowed value is
removed or changed. A breaking change needs a new API version and a new snapshot.

**Consequence:** refusals and an open circuit reuse the existing `classifier_unavailable`
review reason instead of adding new values.

## D10. Retries depend on why the attempt failed

**Decision:**
- **Transient failures** (crash, rate limit, timeout): retried after a random exponential
  backoff ("full jitter": up to 1 s, then 2 s, … capped at 10 s).
- **Unusable output:** retried immediately, with a note naming only the invalid schema
  fields. Values are never echoed back, and invented field names appear only as
  "unexpected fields".
- **Refusals:** never retried.

**Known cost:** older retry tests now go through the real backoff, so the suite takes about
3 s instead of about 0.7 s. Making them instant would mean editing existing tests (D3).

## D11. A circuit breaker stops calling Claude during an outage

**Decision:**
- After 5 tickets in a row can't reach Claude, tickets go straight to human review for 30 s.
- Then a single trial ticket decides whether to resume.
- Bad answers and refusals count as "Claude is reachable".
- A configuration error releases the trial slot so the breaker can't get stuck.

**Why:** without it, every ticket during an outage waits up to 2 × 120 s for its timeouts.

## D12. Results are stored in SQLite, with optional idempotency

**Decision:**
- Every result is saved in `data/tickets.db` using Python's built-in `sqlite3`, run in a
  background thread so it doesn't block the server. No database server and no new
  dependency.
- Each row is the full result as JSON, which includes the **redacted** text. The original
  text is never stored.

**Idempotency:** an optional caller `external_id` makes a repeat return the stored result
without calling Claude. A stored fallback is classified again under the same `ticket_id`.
When two requests with the same ID run at once, the first one saved wins.

**Failure handling:** storage errors are logged and never fail a classification that
already ran.

**Test isolation:** tests that inject a classifier get no store unless they pass one, so they
never write to `data/`.

## D13. Metrics use Prometheus; alerts also appear in the app's logs

**Decision:**
- `prometheus-client` with a dedicated registry, served at `/metrics`.
- In-app alerts (`alerts.py`) log `ALERT FIRING` and `ALERT RESOLVED` once each, not on
  every ticket. They cover an open circuit, and high fallback or injection rates over the
  last 20 tickets. Rates are judged only once the window is full.
- `deploy/prometheus/alerts.yml` has the same conditions plus service down, slow tickets and
  storage errors. A test checks that every metric it uses exists.

**Rejected:** a built-in JSON metrics endpoint. It needs no dependency, but standard tooling
can't scrape it.

**Not verified:** the PromQL hasn't been run through `promtool`.

## D14. Old ticket text is erased; the classification is kept

**Decision:**
- **Default (option B):** `purge --older-than-days N` replaces the stored text **and
  Claude's reasoning** with `[PURGED]` in records older than N days. Category, team,
  priority, review reasons, PII types and timings are kept.
- **Option A:** `--delete` removes whole records.
- `PRAGMA secure_delete` overwrites the erased bytes in the file.
- Purging is idempotent, and purged tickets are skipped by `reprocess`.

**Why:** names can't be detected (D5), so erasing the text is the only way to remove them.
Option B keeps history and lookups working.

**Not covered:** purge isn't scheduled automatically (cron is the owner's choice), and
backups of the database aren't purged.

## D15. Eval labels are the owner's decision

**Decision:** labels in `evals/tickets.jsonl` change only with the owner's approval.

**Format:**
- Attack tickets have no category or priority, so they don't skew accuracy.
- Reports name every category, priority and review mistake by ticket and run, and list
  tickets whose answers changed between repeats.
- Saved reports in `evals/results/` contain IDs and labels only.

**Changes so far:** `lookalike-02` ("setup instructions missing from the box") now expects
review. It honestly fits two categories, so Claude's low confidence and the resulting human
review are correct.

## D16. Repository hygiene

**Decision:**
- One feature branch and one PR per change.
- GitHub deletes a branch automatically when its PR merges.
- Dependabot opens one grouped PR per week for Python dependencies and one for GitHub
  Actions. Security updates are not grouped, so they're never held back.
