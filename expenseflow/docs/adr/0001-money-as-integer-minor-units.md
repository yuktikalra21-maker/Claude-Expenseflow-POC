# 0001 — Money as integer minor units

- **Status:** Accepted
- **Date:** 2026-07-31
- **Context tags:** money, precision, data model

## Context

ExpenseFlow records monetary amounts and normalises every expense to a base
currency (INR) on write. Amounts are submitted, converted through an FX rate,
persisted, and read back for approval — so the representation chosen for money
flows through the schema (`original_amount_minor`, `base_amount_minor`), the FX
conversion, the API contract, and every downstream reader.

The question is how to represent a monetary amount. The failure mode we must
avoid is silent precision loss: binary floating point cannot represent most
decimal fractions exactly, so `0.1 + 0.2 != 0.3`, and repeated arithmetic
(especially FX multiplication and rounding) accumulates drift. For money, a
value that is off by a fraction of a paisa is a correctness bug, not a rounding
nicety.

## Decision

Store and move money as **integer minor units** — paise for INR, cents for
other currencies — never as a float.

- Schema columns `original_amount_minor` and `base_amount_minor` are integers
  (`app/models.py`).
- The API contract (`app/schemas.py`) accepts `amount_minor` as a positive
  integer and returns integer minor units.
- FX conversion (`app/fx.py`) uses `Decimal` for the intermediate arithmetic,
  applies `ROUND_HALF_UP` **once** at write time, and returns an integer paise
  value. The rate itself is persisted as a fixed-precision **string**, not a
  float, so a conversion is exactly reproducible.

Base currency (INR) is a system constant, not a per-row column, so no two rows
can disagree on what "base" means.

## Alternatives considered

- **Floating-point (`float` / SQLite `REAL`).** Simplest to write, but loses
  precision on decimal fractions and drifts under FX multiplication and
  rounding. Rejected — this is exactly the correctness bug we are avoiding.
- **Decimal all the way to storage.** Correct precision, but SQLite has no
  native decimal type, so it would serialise to text or float anyway, and every
  read would have to parse it back before arithmetic. We still use `Decimal` for
  the *conversion step*, but integers are a cleaner, unambiguous storage and
  wire format. Rejected as the persisted representation.
- **Store a formatted string like `"45.00"`.** Human-readable but useless for
  arithmetic without parsing, and invites inconsistent formatting. Rejected.

## Consequences

**Positive**

- No floating-point drift; arithmetic on amounts is exact integer math.
- Storage and wire format are unambiguous — an integer is an integer in SQLite,
  JSON, and Python.
- Rounding happens exactly once, at a single documented point (`ROUND_HALF_UP`
  in `app/fx.py`), and `original_amount_minor` + `fx_rate` make any converted
  amount reconstructable for audit.

**Negative / trade-offs**

- Clients must submit and interpret minor units (e.g. `4500`, not `45.00`);
  any display layer is responsible for formatting back to major units. This is
  documented in the schema field descriptions and the README.
- The minor-unit scale is assumed to be 2 decimal places. Zero-decimal
  currencies (e.g. JPY) or three-decimal currencies would need explicit
  per-currency handling, which the PoC does not implement.
- Every developer touching money must remember the convention; a value read as
  "45" means 45 paise, not 45 rupees.

## References

- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) §1 (schema) and §4.2 (rounding).
- `app/models.py`, `app/schemas.py`, `app/fx.py`.
