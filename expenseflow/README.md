# ExpenseFlow API

A small expense submission and approval API. Proof of concept, not production.

One user journey: **submit** an expense, **normalise** its amount to a base
currency (INR) on write, then **approve** or **reject** it. Reads are open;
writes require an API key. Every `description` is PII-masked on the way out, and
an optional LLM-backed spending summary is available.

## What it does

- `POST /expenses` fetches the FX rate for the submitted currency, converts the
  amount to INR *paise* (integer minor units, never float), and stores the
  expense as `pending`. The write is atomic: if the FX rate is unavailable the
  request fails with `503` and no row is written.
- Expenses can be listed (newest first, optionally filtered by status) or
  fetched by id. `description` is PII-masked (emails, phone numbers,
  card/account numbers) on every response.
- `approve` / `reject` are terminal and legal only from `pending`. A second
  decision returns `409 Conflict` with the current status. The transition is a
  single conditional `UPDATE`, so concurrent decisions cannot both win.
- `GET /reports/insights` asks Claude for a structured spending summary. It is
  best-effort and never raises — a failed or unconfigured model call still
  returns `200` with a safe fallback object.

## Stack

- Python 3.12, FastAPI, Uvicorn
- SQLAlchemy ORM on SQLite (file: `expenseflow.db`)
- httpx for the external FX rate call
- pydantic v2 for request/response models
- python-dotenv for configuration
- anthropic SDK for the optional insights endpoint
- pytest for tests

Layout: `app/main.py`, `app/db.py`, `app/models.py`, `app/schemas.py`,
`app/routes.py`, plus `app/fx.py` (FX conversion), `app/auth.py` (API-key
guard), `app/sanitize.py` (PII masking), and `app/insights.py` (LLM summary).

## Setup (Windows)

From the project root (`expenseflow\`), create and activate a virtual
environment, then install the dependencies:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

> PowerShell instead of `cmd`? Activate with `.venv\Scripts\Activate.ps1`. If
> activation is blocked, run
> `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first.

`requirements.txt` does not pin the `anthropic` SDK; the `GET /reports/insights`
endpoint imports it. Install it if you intend to use insights:

```bat
python -m pip install anthropic
```

## Configure `.env`

Copy the template and fill it in. `.env` is git-ignored and read by
python-dotenv at startup (`app/db.py`).

```bat
copy .env.example .env
```

| Variable            | Required            | Purpose |
|---------------------|---------------------|---------|
| `API_KEY`           | Yes, for writes     | Shared secret expected in the `X-API-Key` header on `POST /expenses`, approve, and reject. If unset, **all writes fail closed with `401`**. |
| `FX_API_URL`        | For non-INR writes  | Base URL of the external FX rate service. INR submissions need no FX call. If unset, a non-INR submission fails closed with `503`. |
| `FX_API_KEY`        | Optional            | Only if your FX provider requires one. |
| `DATABASE_URL`      | Optional            | Override the SQLite location. Defaults to `sqlite:///expenseflow.db`. |
| `ANTHROPIC_API_KEY` | For insights        | Read by the `anthropic` client for `GET /reports/insights`. Without it, the endpoint returns its safe fallback object. |

The FX service is expected to answer a request of the form
`GET {FX_API_URL}?base={CURRENCY}&symbols=INR` with a body shaped like
`{"rates": {"INR": <rate>}}`, where `<rate>` is INR per one unit of the
submitted currency.

## Run the server

```bat
python -m uvicorn app.main:app --reload
```

Tables are created on startup, so no migration step is needed. The server
listens on `http://127.0.0.1:8000` by default.

- Interactive Swagger UI: `http://127.0.0.1:8000/docs` — a custom page that
  auto-attaches the server's `API_KEY` as `X-API-Key` so write endpoints work
  without a manual key prompt. **Local-dev convenience only:** the key is
  embedded in the page's JavaScript, so do not expose `/docs` remotely.
- ReDoc: `http://127.0.0.1:8000/redoc`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

## Run the tests

```bat
python -m pytest -q
```

## Endpoint reference

Money is always in integer minor units (paise/cents). `base_amount_minor` is
the canonical INR value. Authenticated endpoints require the `X-API-Key` header.

### `GET /health`

Liveness probe.

```json
{ "status": "ok" }
```

### `POST /expenses` 🔒

Submit an expense. Requires `X-API-Key`.

Request body (`ExpenseCreate`):

| Field          | Type   | Rules |
|----------------|--------|-------|
| `description`  | string | Non-blank (trimmed); may contain PII. |
| `amount_minor` | int    | Positive integer, as submitted, in minor units. |
| `currency`     | string | 3-letter ISO-4217 code; normalised to upper-case. |

```bash
curl -X POST http://127.0.0.1:8000/expenses \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"description": "Team lunch", "amount_minor": 4500, "currency": "USD"}'
```

Responses:

- `201 Created` — the full expense object (see `ExpenseOut` below), `status`
  `pending`.
- `401 Unauthorized` — missing or invalid API key.
- `422 Unprocessable Entity` — body fails validation.
- `503 Service Unavailable` — FX rate unavailable/invalid; no row written.

### `GET /expenses`

List expenses, newest first. Optional `status` query param filters by
`pending | approved | rejected` (any other value → `422`). Each `description`
is PII-masked.

```bash
curl "http://127.0.0.1:8000/expenses?status=pending"
```

```json
{ "expenses": [ { /* ExpenseOut */ } ] }
```

### `GET /expenses/{expense_id}`

Fetch a single expense by id. `description` is PII-masked.

- `200 OK` — an `ExpenseOut`.
- `404 Not Found` — no such expense.

### `POST /expenses/{expense_id}/approve` 🔒

Approve a pending expense. Requires `X-API-Key`. Optional body
`{ "note": "<reviewer note>" }` (`DecisionRequest`).

- `200 OK` — the updated `ExpenseOut`, `status` `approved`.
- `401 Unauthorized` — missing or invalid API key.
- `404 Not Found` — no such expense.
- `409 Conflict` — already decided; detail carries the current status.

### `POST /expenses/{expense_id}/reject` 🔒

Reject a pending expense. Same contract as approve, resulting in `status`
`rejected`.

### `GET /reports/insights`

LLM-generated structured spending summary over all recorded expenses.
Best-effort — always returns `200`.

```json
{
  "insight": {
    "summary": "…",
    "bullets": ["…", "…", "…"]
  }
}
```

Descriptions are PII-masked and passed as untrusted, delimited data (never as
instructions) before the model call. If the model is unavailable or
`ANTHROPIC_API_KEY` is unset, a safe fallback object of the same shape is
returned.

### `GET /expenses/recent/{n}`

Returns the `n` most recent expenses. ⚠️ Marked in the source as containing
deliberate bugs — not part of the core journey.

### The `ExpenseOut` object

Returned by the expense endpoints:

| Field                   | Type          | Notes |
|-------------------------|---------------|-------|
| `id`                    | int           | |
| `description`           | string        | PII-masked on output. |
| `original_amount_minor` | int           | Amount as submitted, minor units. |
| `original_currency`     | string        | ISO-4217 code as submitted. |
| `base_amount_minor`     | int           | Amount normalised to INR paise. Canonical. |
| `fx_rate`               | string        | Fixed-precision rate used (`"1"` for INR). |
| `status`                | string        | `pending` \| `approved` \| `rejected`. |
| `created_at`            | string        | ISO-8601 UTC. |
| `decided_at`            | string \| null | ISO-8601 UTC; `null` while pending. |
