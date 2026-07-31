"""Streamlit front-end for the ExpenseFlow API.

A thin UI over the FastAPI service: submit an expense, browse existing expenses,
and generate spending insights. All calls go over httpx to the API whose base
URL is read from the ``API_BASE`` environment variable (default
``http://127.0.0.1:8000``); the app holds no business logic or database of its
own. Write actions send the shared secret from ``API_KEY`` in the ``X-API-Key``
header, matching the API's auth. Connection and API errors are caught and shown
as friendly messages rather than tracebacks.

Run with:  streamlit run ui/app.py
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

import httpx
import streamlit as st
from dotenv import load_dotenv

# Load .env so the UI reads the same API_BASE / API_KEY as the API (app.db also
# calls load_dotenv), letting a single .env configure both processes.
load_dotenv()

API_BASE: str = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")
API_KEY: str | None = os.getenv("API_KEY")
_TIMEOUT_SECONDS: float = 10.0


def _headers() -> dict[str, str]:
    """Return request headers, including the API key for writes when configured."""
    return {"X-API-Key": API_KEY} if API_KEY else {}


def call_api(method: str, path: str, **kwargs) -> tuple[object | None, str | None]:
    """Call the API and return ``(data, error)``.

    Exactly one side is populated: on success ``(parsed_json, None)``; on any
    transport failure or non-2xx response ``(None, friendly_message)``. Nothing
    raises out of here, so callers never surface a stack trace to the user.
    """
    try:
        with httpx.Client(base_url=API_BASE, timeout=_TIMEOUT_SECONDS) as client:
            response = client.request(method, path, headers=_headers(), **kwargs)
    except httpx.RequestError:
        return None, (
            f"Couldn't reach the ExpenseFlow API at {API_BASE}. "
            "Is the server running? Start it with "
            "`python -m uvicorn app.main:app --reload`."
        )

    if response.status_code >= 400:
        detail: object
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text or "(no body)"
        if response.status_code == 401:
            return None, (
                "The API rejected the request as unauthorized (401). Set the "
                "`API_KEY` environment variable for this UI to the same value "
                "the API is configured with, then reload."
            )
        return None, f"The API returned {response.status_code}: {detail}"

    try:
        return response.json(), None
    except ValueError:
        return None, "The API returned a response that wasn't valid JSON."


def _to_minor_units(amount: Decimal) -> int:
    """Convert a major-unit amount (e.g. rupees) to integer minor units (paise)."""
    return int((amount * 100).to_integral_value())


def _format_money(minor_units: int, currency: str) -> str:
    """Format integer minor units as a 2-decimal amount, for DISPLAY ONLY.

    Uses ``Decimal`` so there's no float drift in the shown value. INR is shown
    with a ``₹`` prefix; other currencies are suffixed with their code. The
    stored value stays an integer — this only affects presentation.
    """
    major = (Decimal(minor_units) / 100).quantize(Decimal("0.01"))
    if currency.upper() == "INR":
        return f"₹{major:,.2f}"
    return f"{major:,.2f} {currency.upper()}"


_STATUS_LABELS: dict[str, str] = {
    "pending": "🟡 Pending",
    "approved": "🟢 Approved",
    "rejected": "🔴 Rejected",
}


def _status_label(status: str) -> str:
    """Return a clear, human-friendly label for a raw status string."""
    return _STATUS_LABELS.get(status, status.capitalize())


st.set_page_config(page_title="ExpenseFlow", page_icon="💸")
st.title("💸 ExpenseFlow")
st.caption("Submit expenses, review them, and generate spending insights.")
st.caption(f"Connected to API at `{API_BASE}`")

# In-flight state for the submit button, plus a one-shot flash message that
# survives the rerun used to re-enable the button (see the submit section).
st.session_state.setdefault("submitting", False)
st.session_state.setdefault("flash", None)

if not API_KEY:
    st.warning(
        "`API_KEY` is not set for this UI, so submitting or deciding expenses "
        "will be rejected (401). Set it to the API's key and reload.",
        icon="⚠️",
    )

# --- Submit a new expense --------------------------------------------------
st.header("Submit an expense")

# Render (and clear) any message left by the previous submit run.
if st.session_state.flash:
    level, text = st.session_state.flash
    getattr(st, level)(text)
    st.session_state.flash = None

with st.form("submit_expense", clear_on_submit=False):
    amount = st.number_input(
        "Amount", min_value=0.01, value=100.00, step=1.00, format="%.2f"
    )
    currency = st.text_input("Currency (ISO-4217)", value="INR", max_chars=3)
    category = st.text_input("Category", value="")
    st.caption(
        "Note: the API has no category field yet, so category is not sent or "
        "stored — it's shown here for future use only."
    )
    description = st.text_area("Description", value="")
    submitted = st.form_submit_button(
        "Submitting…" if st.session_state.submitting else "Submit expense",
        disabled=st.session_state.submitting,
    )

# A submit flips into the in-flight state and reruns once, so the button below
# re-renders disabled before the (blocking) request is sent on the next run.
if submitted and not st.session_state.submitting:
    st.session_state.submitting = True
    st.rerun()

if st.session_state.submitting:
    error_message: str | None = None
    try:
        amount_minor = _to_minor_units(Decimal(str(amount)))
    except InvalidOperation:
        amount_minor = 0
    if amount_minor <= 0:
        error_message = "Amount must be greater than zero."
    elif not description.strip():
        error_message = "Description is required."
    elif len(currency.strip()) != 3 or not currency.strip().isalpha():
        error_message = "Currency must be a 3-letter ISO-4217 code, e.g. INR."

    if error_message:
        st.session_state.flash = ("error", error_message)
    else:
        payload = {
            "description": description.strip(),
            "amount_minor": amount_minor,
            "currency": currency.strip().upper(),
        }
        with st.spinner("Submitting…"):
            data, error = call_api("POST", "/expenses", json=payload)
        if error:
            st.session_state.flash = ("error", error)
        else:
            st.session_state.flash = (
                "success",
                f"Submitted expense #{data['id']} — {_status_label(data['status'])}.",
            )

    # Re-enable the button and show the result on the next run.
    st.session_state.submitting = False
    st.rerun()

# --- List existing expenses ------------------------------------------------
st.header("Expenses")
if st.button("Refresh expenses"):
    st.rerun()

data, error = call_api("GET", "/expenses")
if error:
    st.error(error)
else:
    expenses = data.get("expenses", []) if isinstance(data, dict) else []
    if not expenses:
        st.info("No expenses yet. Submit one above.")
    else:
        rows = [
            {
                "id": e["id"],
                "description": e["description"],
                "original": _format_money(
                    e["original_amount_minor"], e["original_currency"]
                ),
                "base (INR)": _format_money(e["base_amount_minor"], "INR"),
                "fx_rate": e["fx_rate"],
                "status": _status_label(e["status"]),
                "created_at": e["created_at"],
                "decided_at": e["decided_at"],
            }
            for e in expenses
        ]
        st.dataframe(rows, width="stretch", hide_index=True)

# --- Insights --------------------------------------------------------------
st.header("Insights")
if st.button("Generate insights"):
    data, error = call_api("GET", "/reports/insights")
    if error:
        st.error(error)
    else:
        insight = data.get("insight", {}) if isinstance(data, dict) else {}
        st.subheader("Summary")
        st.write(insight.get("summary", "(no summary returned)"))
        bullets = insight.get("bullets", [])
        if bullets:
            for bullet in bullets:
                st.markdown(f"- {bullet}")
        else:
            st.caption("No bullet points were returned.")
