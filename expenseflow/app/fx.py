"""FX rate fetch and base-currency conversion for ExpenseFlow.

Isolates the one external network dependency (``docs/ARCHITECTURE.md`` §4.1) so
the ``POST /expenses`` hot path can fail closed and so the call is a single,
stubbable seam in tests. Base currency is INR.

Two things live here and nothing else:

- :func:`get_rate` — the httpx call that returns *units of INR per one unit of
  the submitted currency*, as a :class:`~decimal.Decimal`. It validates that the
  rate is present, parseable, and strictly positive, and raises :class:`FXError`
  on any timeout, non-2xx, malformed body, or non-positive rate. INR short-
  circuits to ``Decimal("1")`` with no network call.
- :func:`to_base_minor` — the ``Decimal`` conversion with a single documented
  rounding mode (``ROUND_HALF_UP``, §4.2), returning integer INR paise.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx

# The single base currency all amounts are normalised to on write.
BASE_CURRENCY: str = "INR"

# Bound the external call so a slow FX service cannot hang the request.
_FX_TIMEOUT_SECONDS: float = 5.0


class FXError(Exception):
    """Raised when an FX rate cannot be obtained or is not usable.

    The route layer maps this to ``503 Service Unavailable`` and writes no row,
    keeping ``POST /expenses`` atomic (ARCHITECTURE.md §4.1).
    """


def get_rate(currency: str) -> Decimal:
    """Return the INR-per-``currency`` rate as a positive :class:`Decimal`.

    ``currency`` is assumed already validated/upper-cased by the schema layer.
    INR returns ``Decimal("1")`` without a network call. Any transport failure,
    non-2xx response, malformed body, or rate that is missing/unparseable/``<= 0``
    raises :class:`FXError` — the caller must fail closed.
    """
    if currency == BASE_CURRENCY:
        return Decimal("1")

    url = os.getenv("FX_API_URL")
    if not url:
        raise FXError("FX_API_URL is not configured")

    try:
        response = httpx.get(
            url,
            params={"base": currency, "symbols": BASE_CURRENCY},
            timeout=_FX_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        raw_rate = payload["rates"][BASE_CURRENCY]
    except httpx.HTTPError as exc:
        raise FXError(f"FX request failed: {exc}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise FXError(f"FX response malformed: {exc}") from exc

    try:
        rate = Decimal(str(raw_rate))
    except InvalidOperation as exc:
        raise FXError(f"FX rate not parseable: {raw_rate!r}") from exc

    if rate <= 0:
        raise FXError(f"FX rate not positive: {rate}")

    return rate


def to_base_minor(amount_minor: int, rate: Decimal) -> int:
    """Convert ``amount_minor`` to INR paise using ``rate``, rounded once.

    Uses ``Decimal`` throughout and ``ROUND_HALF_UP`` a single time at write
    (ARCHITECTURE.md §4.2), never float, and returns an integer paise value.
    """
    converted = Decimal(amount_minor) * rate
    return int(converted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
