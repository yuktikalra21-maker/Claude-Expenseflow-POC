"""API-key authentication for write endpoints.

Write actions (submit / approve / reject) require a shared secret sent in the
``X-API-Key`` request header. The expected key is read from the ``API_KEY``
environment variable (never hardcoded; loaded from ``.env`` via python-dotenv in
:mod:`app.db`). Reads are intentionally left open — only mutations are gated.

The check fails closed: if ``API_KEY`` is unset the server has no valid key to
match, so every write is rejected with ``401`` rather than silently accepted.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


def require_api_key(
    x_api_key: str | None = Header(
        default=None, alias="X-API-Key", include_in_schema=False
    ),
) -> None:
    """Reject the request with ``401`` unless a valid ``X-API-Key`` is present.

    Compares the supplied header against ``API_KEY`` using a constant-time
    comparison so a wrong key cannot be probed byte-by-byte via timing. Returns
    ``None`` on success (used only for its side effect as a dependency).
    """
    expected = os.getenv("API_KEY")
    if not expected or x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key",
            headers={"WWW-Authenticate": "API-Key"},
        )
