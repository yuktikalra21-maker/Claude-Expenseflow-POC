"""ORM models for ExpenseFlow.

Defines the ``Expense`` model, mapping the ``expenses`` table described in
``docs/ARCHITECTURE.md`` §1. Money is stored as integer minor units (paise),
never float. Timestamps are ISO-8601 UTC text because SQLite has no native
datetime type. FX rate is stored as a fixed-precision string so a conversion is
exactly reproducible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# Allowed workflow states. SQLite has no native enum, so the values are
# enforced at two layers: a CHECK constraint at the DB level (below) and
# pydantic validation at the API boundary (app.schemas).
STATUS_PENDING: str = "pending"
STATUS_APPROVED: str = "approved"
STATUS_REJECTED: str = "rejected"
ALLOWED_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (default for timestamps)."""
    return datetime.now(timezone.utc).isoformat()


class Expense(Base):
    """A single submitted expense, normalised to INR base currency on write.

    ``status`` transitions only from ``pending`` to a terminal ``approved`` or
    ``rejected`` (see ARCHITECTURE.md §4.3). The DB-level CHECK constraint below
    guards against any value outside :data:`ALLOWED_STATUSES` reaching storage.
    """

    __tablename__ = "expenses"
    __table_args__ = (
        # Enforce the status enum at the storage layer. Must be kept in sync
        # with ALLOWED_STATUSES above.
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_expenses_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    description: Mapped[str] = mapped_column(String, nullable=False)

    # Amount as submitted, in minor units of ``original_currency`` (paise/cents).
    original_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    original_currency: Mapped[str] = mapped_column(String, nullable=False)

    # Amount normalised to INR paise at write time — the canonical value.
    base_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)

    # Conversion rate used, as a fixed-precision string ("1" when already INR).
    fx_rate: Mapped[str] = mapped_column(String, nullable=False)

    status: Mapped[str] = mapped_column(
        String,
        nullable=False,
        default=STATUS_PENDING,
        server_default=STATUS_PENDING,
    )

    # ISO-8601 UTC text. Defaults to submission time.
    created_at: Mapped[str] = mapped_column(
        String, nullable=False, default=_utcnow_iso
    )

    # ISO-8601 UTC text; NULL while pending (natural "no decision yet" signal).
    decided_at: Mapped[str | None] = mapped_column(String, nullable=True)

    def __repr__(self) -> str:
        """Return a concise debug representation."""
        return (
            f"Expense(id={self.id!r}, status={self.status!r}, "
            f"base_amount_minor={self.base_amount_minor!r})"
        )
