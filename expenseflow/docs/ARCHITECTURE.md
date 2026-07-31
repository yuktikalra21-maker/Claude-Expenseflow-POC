# ExpenseFlow — Architecture

ExpenseFlow is a PoC expense submission + approval API. It covers one user journey:
**submit an expense → convert it to the base currency (INR) → approve or reject it.**

Stack: Python 3.12, FastAPI, Uvicorn, SQLAlchemy on SQLite (`expenseflow.db`), httpx for the
external FX rate call, pydantic v2, pytest.

Conventions that shape the design:
- Money is stored as **integer minor units** (paise / cents), never float.
- Base currency is **INR**; every amount is **normalised to base on write**.
- Secrets come from environment variables via `python-dotenv`; nothing is hardcoded.

Two behaviors are load-bearing for correctness and are decided explicitly (see §4):
- **FX down at submission → fail closed.** No row is written; `POST /expenses` returns `503`.
  There is no half-converted expense, because `base_amount_minor` is never NULL or guessed.
- **Decisions are terminal.** `approved` and `rejected` cannot change; re-approving an approved
  expense (or flipping approve↔reject) returns `409 Conflict`, not a silent success.

---

## 1) Database schema: `expenses` table

| Column | Type | Why it exists |
|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` | Surrogate key (SQLite rowid alias). No business meaning leaks through it. |
| `description` | `TEXT NOT NULL` | What the expense was for. Required so no blank submissions. |
| `original_amount_minor` | `INTEGER NOT NULL` | Amount **as submitted**, in minor units of `original_currency` (cents/paise). Integer per "money never float". Audit trail of what the user actually typed. |
| `original_currency` | `TEXT NOT NULL` | ISO-4217 code (e.g. `USD`). Interprets `original_amount_minor` and records what was converted. |
| `base_amount_minor` | `INTEGER NOT NULL` | Amount normalised to **INR paise** at write time. Canonical value all downstream logic reads. |
| `fx_rate` | `TEXT NOT NULL` | Rate used for the conversion, stored as a fixed-precision **string** (not float) so a conversion is exactly reproducible/auditable. `"1"` when original is INR. |
| `status` | `TEXT NOT NULL DEFAULT 'pending'` | Workflow state: `pending` \| `approved` \| `rejected`. Enum enforced in the app layer (SQLite has no native enum). |
| `created_at` | `TEXT NOT NULL` | Submission time, ISO-8601 UTC text (SQLite has no real datetime) — sortable, unambiguous. |
| `decided_at` | `TEXT NULL` | Approve/reject time. NULL while pending — natural "no decision yet" signal. |

Notes:
- Base currency (INR) is a **system constant**, not a column — a per-row base would let rows
  disagree on what "base" means.
- No floats anywhere; `fx_rate` as a fixed-precision string preserves exact `original → base`
  reconstruction.

---

## 2) Endpoints

Scope is limited to the one journey. Approve/reject are two explicit `POST` actions.

| Method | Path | Request body | Success | Errors |
|---|---|---|---|---|
| `POST` | `/expenses` | `{ "description": str, "amount_minor": int (>=0), "currency": str (ISO-4217) }` | `201` full expense object | `422` invalid body; **`503`** FX unavailable/invalid — *no row written*, safe to retry |
| `GET` | `/expenses/{id}` | — | `200` full expense object | `404` not found |
| `GET` | `/expenses` | query: optional `?status=pending\|approved\|rejected` | `200` `{ "expenses": [ ... ] }` | — |
| `POST` | `/expenses/{id}/approve` | optional `{ "note": str }` | `200` updated expense | `404` missing; **`409`** already decided (body carries current status) |
| `POST` | `/expenses/{id}/reject` | optional `{ "note": str }` | `200` updated expense | `404` missing; **`409`** already decided (body carries current status) |

`POST /expenses` behavior: fetch FX rate via httpx, compute `base_amount_minor`, persist with
`status="pending"`. Approve/reject are legal **only from `pending`**.

Full expense object shape:
`id, description, original_amount_minor, original_currency, base_amount_minor, fx_rate, status, created_at, decided_at`.

---

## 3) File layout

Matches the layout mandated in `CLAUDE.md`.

| File | Responsibility |
|---|---|
| `app/main.py` | FastAPI app instance, `.env` loading (python-dotenv), table creation on startup, router registration. Thin. |
| `app/db.py` | SQLAlchemy engine (SQLite `expenseflow.db`), `SessionLocal`, `Base`, `get_db` dependency. No business logic. |
| `app/models.py` | `Expense` ORM model mapping the table above (integer money columns, text timestamps/currency/status). |
| `app/schemas.py` | pydantic v2 models: `ExpenseCreate`, `ExpenseOut`, `DecisionRequest`. Input validation (currency format, `amount_minor >= 0`, status enum). |
| `app/routes.py` | The endpoints above, each with a docstring. Holds FX fetch + conversion wiring and state-transition rules. |
| `app/fx.py` | The httpx FX call + `Decimal`-based conversion helper. Keeps network + rounding logic isolated and stubbable in tests. (Uses httpx, already in the stack — no new dependency.) |
| `tests/` | pytest via FastAPI `TestClient` with the FX call **stubbed** (no network): happy path plus the edge cases in §4. |
| `requirements.txt` | Pins the named stack (fastapi, uvicorn, sqlalchemy, httpx, pydantic, python-dotenv, pytest). |
| `.env.example` | Documents `FX_API_URL` (+ optional `FX_API_KEY`). The real `.env` is git-ignored. |

---

## 4) Edge-case decisions

### 4.1 FX service down / slow / malformed — what happens to `base_amount_minor`?

The FX rate is an external httpx call in the hot path of `POST /expenses`, and `base_amount_minor`
is a `NOT NULL` column the brief requires to be normalised *on write*. There is no valid partial
state: we either have a real rate or we have nothing worth storing.

- **Decision: fail closed.** Set an explicit httpx timeout; wrap the call; validate the returned
  rate is present, parseable, and positive. On any failure (timeout, non-2xx, malformed or ≤0 rate)
  return **`503 Service Unavailable`** and **write no row** — the request is atomic, so a failed
  conversion leaves the DB untouched. `base_amount_minor` therefore never holds a NULL, a `0`, or a
  guessed value.
- **Client contract:** the submission is safe to retry; because nothing was written, a retry cannot
  create a duplicate from the failed attempt.
- **Rejected alternative:** accept the expense now with `base_amount_minor` NULL and backfill via a
  retry/reconciliation job. This violates the "normalised on write" invariant, forces the column
  nullable (so every downstream reader must handle "not yet converted"), and adds a background job —
  unjustified complexity for a PoC, trading a loud, recoverable failure for a silent, wrong number.

### 4.2 Rounding to base minor units

`original_amount_minor × fx_rate` rarely lands on a whole paisa; naive float math drifts and breaks
the integer-money rule. Convert with `Decimal`, apply one documented rounding mode (`ROUND_HALF_UP`)
once at write time, and store an integer. Persisting both `original_amount_minor` and `fx_rate`
makes any dispute reconstructable.

### 4.3 Re-deciding an already-decided expense (+ concurrent decisions)

Can an already-approved expense be approved again, and should the API allow it? **No.** `approved`
and `rejected` are **terminal** states; the only legal decision transition is *from* `pending`.

- A second approve, or an approve-then-reject flip, returns **`409 Conflict`** with the current
  status in the body — not a silent `200`. Treating re-approval as an idempotent no-op is rejected
  because it would also silently permit reject-after-approve and re-stamp `decided_at`, masking real
  workflow bugs. Terminal decisions are immutable.
- This also closes the concurrency race (two approvers / a double-click). Enforce the transition with
  a single conditional update —
  `UPDATE ... SET status=?, decided_at=? WHERE id=? AND status='pending'` — and branch on
  rows-affected: `0` rows means either the id is missing (`404`) or it was already decided (`409`);
  disambiguate with a follow-up read. Atomic under concurrency on SQLite, so exactly one decision can
  ever win.

#### State machine (authoritative)

```
                approve
pending ─────────────────────▶ approved   (terminal)
   │
   │            reject
   └─────────────────────────▶ rejected   (terminal)

approved / rejected: no outgoing transitions. Any decision attempt → 409.
```
