# ExpenseFlow — Production Gap Analysis

An audit of the current PoC against a production bar. Each area states the
**gap**, a **classification** (🔴 Blocking — must fix before real traffic;
🟡 Deferrable — acceptable to launch without, track as follow-up), and a rough
**effort** estimate (S ≈ <1 day, M ≈ 1–3 days, L ≈ >3 days).

Baseline: single-file SQLite, one shared API key, no CI, no container, no
migrations, best-effort PII masking. This is explicitly a PoC (`CLAUDE.md`), so
the point of this doc is to make the delta to production explicit, not to fault
the current design.

| # | Area | Class | Effort |
|---|------|-------|--------|
| 1 | Authentication & key rotation | 🔴 Blocking | M |
| 2 | Input validation | 🟡 Deferrable | S |
| 3 | Rate limiting | 🔴 Blocking | S–M |
| 4 | Observability & logging | 🔴 Blocking | M |
| 5 | Error handling | 🟡 Deferrable | S |
| 6 | DB migrations & pooling | 🔴 Blocking | M–L |
| 7 | Secrets management | 🔴 Blocking | S–M |
| 8 | Tests & coverage | 🟡 Deferrable | M |
| 9 | Deployment & health checks | 🔴 Blocking | M |
| 10 | Data privacy (expense data) | 🔴 Blocking | M–L |

---

## 1. Authentication & key rotation — 🔴 Blocking — M

**Gap.** A single shared secret (`API_KEY`) gates all writes, compared in
constant time (`app/auth.py`). There is no per-user identity, no concept of who
submitted or approved an expense, no key expiry, and **no rotation path** — the
one key is set via env and changing it requires a redeploy and coordinating
every client at once. Reads are fully unauthenticated: anyone who can reach the
service can list and read every expense (`GET /expenses`, `/expenses/{id}`,
`/reports/insights`). Approver identity is not recorded, so there is no
segregation of duties (the submitter can approve their own expense) and no audit
of *who* decided.

**What production needs.** Real principal identity (OIDC/JWT or per-client API
keys with a store), authenticated reads, distinct submitter vs. approver roles,
and support for overlapping valid keys so rotation is zero-downtime.

**Effort.** M — introduce an identity layer and role checks; store approver on
the row; support ≥2 concurrently-valid keys.

## 2. Input validation — 🟡 Deferrable — S

**Gap.** pydantic covers the core contract well (`app/schemas.py`): non-blank
trimmed `description`, `amount_minor > 0`, 3-letter ISO-4217 `currency`. Missing:
no **upper bound** on `amount_minor` (an absurd 10^18 value is accepted and
flows into `Decimal` math and storage), no **max length** on `description` (an
unbounded blob can be stored and later shipped to the LLM), the `currency` check
validates format but not that it is a *real* ISO-4217 code, and
`GET /expenses/recent/{n}` takes an unbounded (and negative-capable) `n` and is
flagged in-source as deliberately buggy.

**Classification rationale.** Deferrable *only* if paired with the rate limiting
and body-size limits in §3; without those, the unbounded fields become a cheap
DoS/cost vector and this edges toward blocking.

**Effort.** S — add `le=` bounds and `max_length`, validate the currency against
a known set, bound or remove `recent/{n}`.

## 3. Rate limiting — 🔴 Blocking — S–M

**Gap.** None exists. Every endpoint is unthrottled. `POST /expenses` makes a
synchronous outbound httpx call to the FX provider, and `GET /reports/insights`
makes a paid Anthropic API call over *all* rows — both are trivial to abuse into
cost amplification or provider rate-limit exhaustion. No request-size limit
either.

**What production needs.** Per-client/per-IP rate limits (at the proxy or via
middleware), a stricter budget on the LLM endpoint, and a max request body size.

**Effort.** S–M — reverse-proxy limits are quick; app-aware per-key quotas and
LLM budgeting are the larger part.

## 4. Observability & logging — 🔴 Blocking — M

**Gap.** There is essentially no operational telemetry. No structured logging,
no request IDs/correlation, no metrics (latency, error rate, FX failure rate,
LLM cost/latency), no tracing, no error reporting. The only application log is a
`TEMP` `logger.warning` at `app/insights.py:188` that dumps the **entire LLM
payload** on every insights call — which is both noise and a privacy problem
(see §10). Operators currently cannot answer "is FX down?", "what's the p95?",
or "why did that request 500?".

**What production needs.** Structured JSON logs with request correlation, a
metrics endpoint (Prometheus/OTel), error tracking (e.g. Sentry), dashboards and
alerts on the FX `503` rate and the `/health` signal. Remove the `TEMP` log.

**Effort.** M.

## 5. Error handling — 🟡 Deferrable — S

**Gap.** The core paths are handled deliberately and well: FX failure fails
closed with `503` and no row (`app/routes.py`), decisions return `404`/`409`
correctly via a conditional UPDATE, and insights swallow all errors to a safe
fallback. Gaps are around the edges: no global exception handler, so an
unexpected error surfaces as FastAPI's default `500` with no correlation id;
`GET /expenses/recent/{n}` is knowingly buggy; and the broad `except Exception`
in insights can mask real defects (though it is intentional there).

**Effort.** S — add an exception handler that returns a correlation id and logs
the trace, and fix/remove `recent/{n}`.

## 6. Database migrations & pooling — 🔴 Blocking — M–L

**Gap.** Schema is created by `Base.metadata.create_all` on startup
(`app/db.py`) — it only *creates missing* tables and never alters existing ones,
so **there is no migration path**: any schema change requires manual DB
surgery or a rebuild, with no version history or rollback. Storage is a single
SQLite file, which is single-writer and does not support horizontal scaling or
meaningful connection pooling; `check_same_thread=False` is set to make it work
under FastAPI at all. Concurrent writers serialize and can hit "database is
locked".

**What production needs.** A real RDBMS (Postgres) with a managed connection
pool, and Alembic (or equivalent) migrations wired into deploy. The
conditional-UPDATE concurrency design already in `_decide` ports cleanly.

**Effort.** M–L — migrate to Postgres, introduce Alembic and a baseline
migration, configure pooling, update tests.

## 7. Secrets management — 🔴 Blocking — S–M

**Gap.** Secrets come from `.env` via python-dotenv (`app/db.py`). Three
concerns: (a) the custom `/docs` page **embeds the server's `API_KEY` into
client-side JavaScript** (`app/main.py`), leaking it to anyone who loads the
page — must be disabled or gated before any shared deployment; (b) there is no
secret manager / rotation / audit — secrets live as plaintext env/`.env` on the
host; (c) `requirements.txt` is **unpinned** (no versions, no hashes), a supply-
chain risk, and `anthropic` is imported but absent from it entirely.

**What production needs.** Secrets from a managed store (Vault / cloud secrets
manager) injected at runtime, `/docs` locked down, and pinned+hashed
dependencies.

**Effort.** S–M — pin deps and lock `/docs` are quick; wiring a secret manager
is the larger piece.

## 8. Tests & coverage — 🟡 Deferrable — M

**Gap.** The existing suite (`tests/test_api.py`) is solid for a PoC: happy
path, status filtering, 404, terminal-decision 409, 401-writes-nothing, FX
fail-closed, and Decimal FX normalization — all with the FX seam stubbed and an
isolated per-test SQLite DB. Missing: no tests for auth rotation/roles (feature
absent), rate limiting (absent), the insights endpoint and its PII masking, the
`recent/{n}` endpoint, concurrency on `_decide`, or input-bound edge cases. No
**coverage measurement**, and **no CI** at all (`.github` absent) — nothing runs
the suite automatically or blocks a merge.

**Classification rationale.** Deferrable as raw coverage, but the *CI* piece is
effectively blocking once more than one person commits — track it with §9.

**Effort.** M — add CI (run pytest + coverage gate) and fill the missing cases
as the corresponding features land.

## 9. Deployment & health checks — 🔴 Blocking — M

**Gap.** No deployment artifacts exist: no Dockerfile, no CI/CD, no process
manager or worker config; the documented run command uses `--reload` (a dev
server). `GET /health` is a **static liveness** probe only — it returns `ok`
without checking the DB or FX, so it cannot serve as a **readiness** signal (the
app reports healthy while its dependencies are down). No graceful-shutdown
handling beyond the `lifespan` table-create, no resource limits, no rollback
strategy.

**What production needs.** A container image, a real ASGI server behind a proxy
with multiple workers, CI/CD with rollback, and a readiness probe that verifies
DB connectivity (and optionally FX) distinct from liveness.

**Effort.** M.

## 10. Data privacy (expense data) — 🔴 Blocking — M–L

**Gap.** Expense descriptions are user free text that can contain PII (emails,
phones, card/account numbers). Current handling:

- Masking is **output-time only** (`app/routes.py::_masked_out`) — the **raw,
  unmasked PII is stored in the SQLite file in plaintext** and there is no
  encryption at rest. Anyone with the DB file (or an open read endpoint, see §1)
  gets the raw data.
- Masking is **best-effort regex** (`app/sanitize.py`) over three categories;
  it is not a compliance-grade guarantee and will miss formats (IBANs, national
  IDs, names/addresses).
- The `TEMP` log line (`app/insights.py:188`) writes the insight payload to logs
  on every call; even though it is the masked copy, it puts spending data into
  log storage that likely has weaker controls.
- No data-retention policy, no deletion/right-to-erasure path, no access log/
  audit trail on reads, and reads are unauthenticated (§1) — so there is no
  record of who viewed expense data.

**What production needs.** Encrypt sensitive data at rest, authenticate and
audit reads, define retention/erasure, remove the payload log, and treat masking
as defense-in-depth rather than the primary control. If real card numbers are
ever in scope, PCI obligations apply.

**Effort.** M–L.

---

## Blocking summary (fix before real traffic)

1. **Authentication & key rotation** (§1) — identity, authenticated reads,
   rotation, submitter/approver separation.
2. **Rate limiting** (§3) — throttle all endpoints; budget the FX and LLM calls.
3. **Observability & logging** (§4) — structured logs, metrics, alerts; remove
   the payload log.
4. **DB migrations & pooling** (§6) — Postgres + Alembic + pooling.
5. **Secrets management** (§7) — managed secrets, lock down `/docs`, pin deps.
6. **Deployment & health checks** (§9) — container, CI/CD, real readiness probe.
7. **Data privacy** (§10) — encryption at rest, audited/authenticated reads,
   retention, remove payload log.

Deferrable, tracked as follow-ups: input-bound hardening (§2), edge-case error
handling (§5), and broader test coverage + CI (§8) — with the caveat that CI and
request-size/rate bounds should ride along with the blocking items they backstop.
