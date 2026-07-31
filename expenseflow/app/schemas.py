"""Pydantic v2 request/response models for ExpenseFlow.

These are the API boundary contracts described in ``docs/ARCHITECTURE.md`` §2–3:

- :class:`ExpenseCreate` — the ``POST /expenses`` request body. The client sends
  a single ``amount_minor``/``currency`` pair; the route layer fetches the FX
  rate and derives the ``original_*``/``base_*`` columns (see ``app.routes``).
- :class:`ExpenseOut` — the full expense object returned by every endpoint,
  serialised straight from the :class:`app.models.Expense` ORM instance.
- :class:`DecisionRequest` — the optional ``{ "note": str }`` body for the
  approve/reject actions.

Validation enforced here (the API-layer half of the two-layer enum/format
guard noted in ``app.models``): ``amount_minor`` is a positive integer and
``currency`` is a 3-letter ISO-4217 code. Money is always integer minor units
(paise/cents), never float.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import ALLOWED_STATUSES


class ExpenseCreate(BaseModel):
    """Request body for ``POST /expenses``.

    ``amount_minor`` and ``currency`` describe the expense *as submitted*; the
    route computes ``base_amount_minor`` from the FX rate before persisting.
    """

    description: str = Field(min_length=1, description="What the expense was for.")
    amount_minor: int = Field(
        gt=0,
        description="Amount as submitted, in minor units (paise/cents). Positive integer.",
    )
    currency: str = Field(description="ISO-4217 currency code, e.g. 'USD'.")

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str) -> str:
        """Reject blank/whitespace-only descriptions after trimming."""
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("description must not be blank")
        return trimmed

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        """Normalise to upper-case and require exactly three ASCII letters."""
        code = value.strip().upper()
        if len(code) != 3 or not code.isalpha() or not code.isascii():
            raise ValueError("currency must be a 3-letter ISO-4217 code")
        return code


class ExpenseOut(BaseModel):
    """Full expense object returned by every endpoint.

    Mirrors the ``expenses`` table (ARCHITECTURE.md §1) one-to-one and is
    populated directly from the :class:`app.models.Expense` ORM instance via
    ``from_attributes``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    original_amount_minor: int
    original_currency: str
    base_amount_minor: int
    fx_rate: str
    status: str
    created_at: str
    decided_at: str | None = None


class DecisionRequest(BaseModel):
    """Optional body for ``POST /expenses/{id}/approve`` and ``.../reject``."""

    note: str | None = Field(default=None, description="Optional reviewer note.")


# Re-exported so callers validating status query params share the single
# source of truth defined on the ORM model.
__all__ = ["ExpenseCreate", "ExpenseOut", "DecisionRequest", "ALLOWED_STATUSES"]
