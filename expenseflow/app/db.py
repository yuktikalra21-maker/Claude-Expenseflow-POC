"""Database wiring for ExpenseFlow.

Holds the SQLAlchemy engine, session factory, declarative ``Base``, the
FastAPI ``get_db`` dependency, and ``init_db`` for table creation. No business
logic lives here — models are defined in ``app.models`` and endpoints in
``app.routes``.

The PoC uses SQLite (file ``expenseflow.db``). The location is read from the
``DATABASE_URL`` environment variable (via python-dotenv) so it can be pointed
elsewhere for tests; it falls back to the project-local SQLite file. This is a
path, not a secret, so a default is safe.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Load .env before reading any configuration so env vars are populated.
load_dotenv()

# SQLite connection URL. Default to a project-local file per the brief.
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///expenseflow.db")

# ``check_same_thread=False`` is required for SQLite under FastAPI, where a
# session may be used across threads within a request lifecycle. Only pass the
# SQLite-specific connect arg when we are actually on SQLite.
_connect_args: dict[str, object] = (
    {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

engine: Engine = create_engine(DATABASE_URL, connect_args=_connect_args)

# Session factory. ``autoflush`` off keeps writes explicit; commits are done by
# the caller (routes) so a failed FX conversion never leaves a half-written row.
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, autocommit=False, autoflush=False
)


class Base(DeclarativeBase):
    """Declarative base class that all ORM models inherit from."""


def get_db() -> Iterator[Session]:
    """Yield a database session for the duration of a request.

    Used as a FastAPI dependency (``Depends(get_db)``). The session is always
    closed when the request finishes, even if the handler raises.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables registered on ``Base.metadata``.

    Imports ``app.models`` for its side effect of registering the ORM models on
    the metadata before creating tables. Safe to call repeatedly — existing
    tables are left untouched.
    """
    from app import models  # noqa: F401  (import registers models on Base)

    Base.metadata.create_all(bind=engine)
