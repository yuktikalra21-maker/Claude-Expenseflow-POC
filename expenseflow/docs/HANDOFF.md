# ExpenseFlow — Handoff

Operational handoff for anyone taking over or deploying ExpenseFlow. For the
design rationale behind each decision, see [`ARCHITECTURE.md`](ARCHITECTURE.md);
for setup/run instructions and the endpoint reference, see the
[`README.md`](../README.md).

---

## 1. What it does

A PoC expense submission + approval API covering one journey: **submit an
expense → normalise its amount to base currency (INR) on write → approve or
reject it.**

- Money is stored as integer minor units (paise), never float.
- Every amount is normalised to INR at write time; `base_amount_minor` is the
  canonical value.
- Reads are open; writes (`submit`, `approve`, `reject`) require an API key.
- Free-text `description` is PII-masked on every response and before it reaches
  logs or the LLM.

---

## 2. How it works

### Request lifecycle

- **App startup** (`app/main.py`): `init_db()` runs on the FastAPI `lifespan`
  hook and creates any missing tables via `Base.metadata.create_all`. There is
  no migration tool — the schema is created from the ORM models. Safe to call
  repeatedly; existing tables are left untouched.
- **`POST /expenses`** (`app/routes.py` → `app/fx.py`):
  1. pydantic validates the body (`app/schemas.py`): non-blank `description`,
     positive integer `amount_minor`, 3-letter ISO-4217 `currency`.
  2. `fx.get_rate(currency)` returns INR-per-currency as a `Decimal`. INR
     short-circuits to `1` with no network call; any other currency makes an
     httpx GET to `FX_API_URL` (5s timeout).
  3. `fx.to_base_minor` converts with `Decimal` + `ROUND_HALF_UP` once, giving
     integer INR paise. The rate is stored as a fixed-precision string so the
     conversion is exactly reproducible.
  4. The row is written with `status="pending"`. **If FX fails at any point
     (timeout, non-2xx, malformed, ≤0 rate), the endpoint returns `503` and
     writes no row** — the write is atomic and safe to retry.
- **Reads** (`GET /expenses`, `GET /expenses/{id}`): serialise straight from the
  ORM row; `description` is PII-masked on the way out (`app/sanitize.py`). The
  stored row is never modified by masking.
- **`approve` / `reject`** (`app/routes.py::_decide`): a single conditional
  `UPDATE ... WHERE id=? AND status='pending'`. Zero rows affected means either
  the id is missing (`404`) or already decided (`409`, with current status in
  the body). This makes decisions terminal and closes the double-click / concurrent-approver race.
- **`GET /reports/insights`** (`app/insights.py`): masks every description,
  wraps the records as untrusted delimited data, and asks Claude for a
  `{summary, bullets[3]}` object. Best-effort — it never raises; on any failure
  or missing key it returns a safe fallback with `200`.

### Component map

| File | Responsibility |
|---|---|
| `app/main.py` | App instance, startup table creation, router registration, custom `/docs`, `/health`. |
| `app/db.py` | Engine, `SessionLocal`, `Base`, `get_db`, `init_db`. Reads `DATABASE_URL`. |
| `app/models.py` | `Expense` ORM model + status CHECK constraint. |
| `app/schemas.py` | pydantic request/response contracts. |
| `app/routes.py` | Endpoints, FX wiring, state-transition rules. |
| `app/fx.py` | httpx FX call + `Decimal` conversion. The one external network dependency. |
| `app/auth.py` | `X-API-Key` guard (constant-time compare). |
| `app/sanitize.py` | PII masking (email/phone/card → typed placeholders). |
| `app/insights.py` | LLM spending summary (optional, best-effort). |

---

## 3. What a deployment engineer needs to know

### Runtime

- Python 3.12. Install `requirements.txt`. **Note:** the `anthropic` SDK is
  imported by `app/insights.py` but is **not** in `requirements.txt` — install
  it separately if `GET /reports/insights` is used, or that endpoint will fail
  to import.
- Start command: `python -m uvicorn app.main:app`. Drop `--reload` outside
  local dev. Put a real ASGI process manager / reverse proxy in front for
  anything beyond the PoC.

### Configuration (all via environment / `.env`, loaded by python-dotenv)

| Variable | Effect if unset |
|---|---|
| `API_KEY` | **All writes fail closed with `401`.** Must be set for the app to be usable. Set to a strong random value. |
| `FX_API_URL` | Non-INR submissions fail closed with `503`. INR-only usage works without it. |
| `FX_API_KEY` | Only needed if your FX provider requires it (currently read only via `.env` template; not sent by `app/fx.py`). |
| `DATABASE_URL` | Defaults to `sqlite:///expenseflow.db` (a file relative to the working directory). |
| `ANTHROPIC_API_KEY` | Insights endpoint returns its fallback object instead of a real summary. |

The FX provider must answer `GET {FX_API_URL}?base={CCY}&symbols=INR` with
`{"rates": {"INR": <positive number>}}`.

### Data & persistence

- Storage is a **single SQLite file** (`expenseflow.db`). It lives on local
  disk relative to the process working directory — **not** in the container by
  default if you want it to survive restarts. Mount it on a persistent volume,
  back it up, and be aware that SQLite is single-writer: this is a PoC-grade
  store, not for horizontal scaling or high write concurrency.
- No migration framework. Schema changes require either recreating the DB or
  hand-migrating; `init_db` only *creates missing* tables, it never alters
  existing ones.
- Do not edit `expenseflow.db` directly (per `CLAUDE.md`).

### Security / operational notes

- Writes are gated by a shared secret in the `X-API-Key` header, compared in
  constant time. Reads are intentionally open.
- **`/docs`** is a custom Swagger page that embeds the server's `API_KEY` in its
  client-side JavaScript so writes work without a manual prompt. This leaks the
  key to anyone who can load the page — **do not expose `/docs` publicly.**
  Disable it or gate it at the proxy in any shared/production environment.
- `app/insights.py:188` logs the full LLM payload at WARNING level (a `TEMP`
  debug line the author marked for removal). Remove it before real use — it
  writes expense descriptions (masked, but still) into your logs on every
  insights call.
- PII masking is best-effort regex (email/phone/card); do not treat it as a
  compliance-grade guarantee.
- Health check: `GET /health` → `{"status": "ok"}`. Use it for liveness probes.

### Tests

- `python -m pytest -q`. Tests use FastAPI's `TestClient` with the FX call
  stubbed (no network). Point `DATABASE_URL` at a throwaway/temp SQLite file for
  test isolation.
