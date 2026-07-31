"""PII redaction for free-text expense fields.

Expense descriptions are user-supplied free text that later flows into logs and
the LLM insights prompt (see :mod:`app.insights`). This module scrubs the
common contact/financial identifiers out of that text before it travels
anywhere, replacing each with a typed placeholder so the redaction is visible
and self-describing:

- email addresses      -> ``[EMAIL]``
- phone numbers        -> ``[PHONE]``
- card/account numbers -> ``[CARD]``

Only free text is touched. :func:`mask_expense` masks the ``description`` field
only and leaves structured numeric fields (``amount_base_minor`` etc.)
untouched — those are validated data, not PII, and must survive intact for the
downstream summary to be correct.

Patterns are compiled once at import. Order of application matters: emails are
redacted first (their local part can contain digit runs that would otherwise
be mis-read as a phone/card), then long card/account runs, then phone numbers
— so each pattern only sees text the earlier ones have already cleaned.
"""

from __future__ import annotations

import re

# Email: local-part@domain.tld. Redacted first so digits inside an address are
# not later mistaken for a phone or card number.
_EMAIL_RE: re.Pattern[str] = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# Card / account number: a long run of 13-19 digits, optionally grouped by
# single spaces or hyphens (e.g. "4111 1111 1111 1111" or "4111111111111111").
# Anchored so it starts and ends on a digit. Run before the phone pattern so a
# 16-digit card is not partially consumed as a phone number.
_CARD_RE: re.Pattern[str] = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")

# Phone number: an optional leading "+" and "(", then 7-15 digits separated by
# spaces, dots, hyphens, or parentheses. The digit count is enforced exactly
# (each repetition is one digit), so short amounts like "4200" never match.
_PHONE_RE: re.Pattern[str] = re.compile(
    r"(?<![\w+])\+?\(?\d(?:[\s.\-()]*\d){6,14}(?![\w])"
)


def mask_pii(text: str) -> str:
    """Return ``text`` with emails, phone numbers, and card/account numbers redacted.

    Each match is replaced by a typed placeholder (``[EMAIL]``, ``[PHONE]``,
    ``[CARD]``). Substitutions are applied in a fixed order — email, then card,
    then phone — so overlapping digit runs are attributed to the most specific
    pattern first. Text with no PII is returned unchanged.
    """
    masked = _EMAIL_RE.sub("[EMAIL]", text)
    masked = _CARD_RE.sub("[CARD]", masked)
    masked = _PHONE_RE.sub("[PHONE]", masked)
    return masked


def mask_expense(expense: dict) -> dict:
    """Return a copy of ``expense`` with its ``description`` field PII-masked.

    The input dict is not mutated. Only ``description`` is passed through
    :func:`mask_pii`; every other field — including structured numerics like
    ``amount_base_minor`` — is copied through unchanged. A missing or
    non-string ``description`` is left as-is.
    """
    masked = dict(expense)
    description = masked.get("description")
    if isinstance(description, str):
        masked["description"] = mask_pii(description)
    return masked
