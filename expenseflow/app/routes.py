"""HTTP endpoints for ExpenseFlow.

Implements the one user journey from ``docs/ARCHITECTURE.md`` §2: submit an
expense, list/fetch expenses, and approve or reject a pending one. Business
rules that live here:

- **Normalise on write.** Every expense stores ``base_amount_minor`` in INR
  paise at submission time. FX conversion is not wired up yet, so the base
  amount is currently set equal to the submitted amount (see the ``TODO`` in
  :func:`create_expense`).
- **Decisions are terminal** (§4.3). Approve/reject are legal only from
  ``pending``; a second decision returns ``409 Conflict`` carrying the current
  status. The transition uses a single conditional ``UPDATE`` so a double-click
  or concurrent approver cannot both win.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import fx
from app.auth import require_api_key
from app.db import get_db
from app.fx import FXError
from app.insights import generate_insight
from app.models import (
    ALLOWED_STATUSES,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    Expense,
)
from app.sanitize import mask_pii
from app.schemas import DecisionRequest, ExpenseCreate, ExpenseOut

router = APIRouter(prefix="/expenses", tags=["expenses"])
reports_router = APIRouter(prefix="/reports", tags=["reports"])


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (decision timestamps)."""
    return datetime.now(timezone.utc).isoformat()


def _masked_out(expense: Expense) -> ExpenseOut:
    """Serialise an expense for API output with its ``description`` PII-masked.

    ``description`` is user-supplied free text and may carry emails, phone
    numbers, or card/account numbers. Masking happens on the way out only — the
    stored row is left untouched, and every other field (amounts, status,
    timestamps) is serialised verbatim.
    """
    out = ExpenseOut.model_validate(expense)
    return out.model_copy(update={"description": mask_pii(out.description)})


@router.post(
    "",
    response_model=ExpenseOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
def create_expense(payload: ExpenseCreate, db: Session = Depends(get_db)) -> Expense:
    """Submit an expense.

    Requires a valid ``X-API-Key`` header. Fetches the FX rate for the submitted
    currency and normalises the amount to INR base paise before persisting with
    ``status="pending"``. Per ARCHITECTURE.md §4.1 the write is atomic: if the FX
    rate is unavailable or invalid the endpoint fails closed with ``503`` and no
    row is written (the conversion happens before the row is created).
    """
    try:
        rate = fx.get_rate(payload.currency)
    except FXError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FX rate unavailable; expense not recorded",
        ) from exc

    base_amount_minor = fx.to_base_minor(payload.amount_minor, rate)
    # Fixed-precision string, never float, so the conversion is reproducible.
    fx_rate = format(rate, "f")

    expense = Expense(
        description=payload.description,
        original_amount_minor=payload.amount_minor,
        original_currency=payload.currency,
        base_amount_minor=base_amount_minor,
        fx_rate=fx_rate,
        status=STATUS_PENDING,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense


@router.get("", response_model=dict[str, list[ExpenseOut]])
def list_expenses(
    db: Session = Depends(get_db),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        description="Optional filter: pending | approved | rejected.",
    ),
) -> dict[str, list[ExpenseOut]]:
    """List expenses, newest first, optionally filtered by status.

    Each ``description`` is PII-masked on output (see :func:`_masked_out`).
    ``category`` filtering is intentionally not supported: there is no category
    column in the schema (see ARCHITECTURE.md §1).
    """
    query = db.query(Expense)
    if status_filter is not None:
        if status_filter not in ALLOWED_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"status must be one of {ALLOWED_STATUSES}",
            )
        query = query.filter(Expense.status == status_filter)
    expenses = query.order_by(Expense.id.desc()).all()
    return {"expenses": [_masked_out(e) for e in expenses]}


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(expense_id: int, db: Session = Depends(get_db)) -> ExpenseOut:
    """Fetch a single expense by id, or ``404`` if it does not exist.

    The ``description`` is PII-masked on output (see :func:`_masked_out`).
    """
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="expense not found"
        )
    return _masked_out(expense)


def _decide(
    expense_id: int, new_status: str, db: Session
) -> Expense:
    """Apply a terminal decision to a pending expense.

    Uses a single conditional ``UPDATE ... WHERE id=? AND status='pending'`` so
    concurrent decisions cannot both succeed (ARCHITECTURE.md §4.3). Raises
    ``404`` if the expense is missing and ``409`` if it is already decided.
    """
    result = db.execute(
        update(Expense)
        .where(Expense.id == expense_id, Expense.status == STATUS_PENDING)
        .values(status=new_status, decided_at=_utcnow_iso())
    )
    db.commit()

    if result.rowcount == 0:
        # The conditional update matched nothing: either the row is absent or it
        # was already decided. Distinguish the two for the correct status code.
        existing = db.get(Expense, expense_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="expense not found"
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "expense already decided",
                "status": existing.status,
            },
        )

    decided = db.get(Expense, expense_id)
    assert decided is not None  # just updated it; guaranteed present
    return decided


@router.post(
    "/{expense_id}/approve",
    response_model=ExpenseOut,
    dependencies=[Depends(require_api_key)],
)
def approve_expense(
    expense_id: int,
    payload: DecisionRequest | None = None,
    db: Session = Depends(get_db),
) -> Expense:
    """Approve a pending expense.

    Legal only from ``pending``; a re-decision returns ``409`` with the current
    status. The optional body may carry a reviewer ``note``.
    """
    return _decide(expense_id, STATUS_APPROVED, db)


@router.post(
    "/{expense_id}/reject",
    response_model=ExpenseOut,
    dependencies=[Depends(require_api_key)],
)
def reject_expense(
    expense_id: int,
    payload: DecisionRequest | None = None,
    db: Session = Depends(get_db),
) -> Expense:
    """Reject a pending expense.

    Legal only from ``pending``; a re-decision returns ``409`` with the current
    status. The optional body may carry a reviewer ``note``.
    """
    return _decide(expense_id, STATUS_REJECTED, db)


@reports_router.get("/insights", response_model=dict[str, dict])
def spending_insights(db: Session = Depends(get_db)) -> dict[str, dict]:
    """Return LLM-generated structured insights about all recorded spending.

    Loads every expense and projects each to ``description``,
    ``amount_base_minor``, and ``status`` (the schema has no ``category``
    column — see :func:`list_expenses`). ``description`` is user-supplied free
    text; :func:`app.insights.generate_insight` PII-masks it (via
    :func:`app.sanitize.mask_expense`) and treats it as untrusted data before
    calling the model. That helper is best-effort and never raises, so a failed
    or unavailable model call still yields a ``200`` with a safe fallback object
    (``{"summary": str, "bullets": [str, str, str]}``).
    """
    expenses = db.query(Expense).order_by(Expense.id.desc()).all()
    rows = [
        {
            "description": e.description,
            "amount_base_minor": e.base_amount_minor,
            "status": e.status,
        }
        for e in expenses
    ]
    return {"insight": generate_insight(rows)}

@router.get('/recent/{n}')
def recent_expenses(n: int, db: Session = Depends(get_db)):
    """Return the n most recent expenses. (Contains bugs on purpose.)"""
    rows = db.query(Expense).order_by(Expense.created_at.desc()).all()
    latest = rows[0].description.upper() if rows else None
    return { 'latest': latest, 'items': rows[:n] }