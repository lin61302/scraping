"""Shared, additive fields for the all-source first inspection pass.

The existing tracker keeps legacy Notion history and strict receipt coverage.
This module adds a small coordination layer around those facts.  It never
promotes a Notion date, a run timestamp, or an inspection flag into verified
coverage.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any


# Freeze the first estate-wide inspection at the latest complete month when
# the campaign began.  A later cycle can advance this single value after the
# first pass has been closed and archived.
FIRST_PASS_TARGET_END = date(2026, 9, 30)
CURRENT_MONTH_START = FIRST_PASS_TARGET_END + timedelta(days=1)

FIRST_PASS_STATES = frozenset(
    {
        "Complete through target",
        "In progress",
        "Blocked",
        "Verified through earlier boundary",
        "Inspected; verification pending",
        "Not inspected",
    }
)


def parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def inspection_addressed(row: dict[str, Any]) -> bool:
    """Return whether current automation has inspected the source at all.

    Legacy Notion stage/date fields are intentionally absent from this test.
    They describe prior history, not whether the new first pass inspected the
    source.
    """

    if str(row.get("Current evidence addressed") or "").strip().lower() == "yes":
        return True
    if parse_date(row.get("First-pass verified through")) or parse_date(
        row.get("Strict verified through")
    ) or parse_date(row.get("Coverage through")):
        return True
    if str(row.get("Last checked") or "").strip():
        return True
    if str(row.get("Baseline status") or "").strip().lower() in {"done", "open"}:
        return True
    progress = str(row.get("Scraping progress") or "").strip().lower()
    if progress and progress not in {"inventory only", "not assessed", "unassessed"}:
        return True
    for key in ("Patchtools status", "Pre-TCG status"):
        value = str(row.get(key) or "").strip().lower()
        if value and value not in {"not assessed", "unassessed"}:
            return True
    return False


def derive_first_pass_state(
    row: dict[str, Any],
    *,
    verified_through: date | None = None,
    target_end: date = FIRST_PASS_TARGET_END,
) -> str:
    """Classify first-pass work without inferring verification from history."""

    explicit = str(row.get("First-pass state") or "").strip()
    if explicit in FIRST_PASS_STATES:
        return explicit

    verified = verified_through or parse_date(row.get("First-pass verified through"))
    verified = verified or parse_date(row.get("Strict verified through"))
    verified = verified or parse_date(row.get("Coverage through"))
    if verified and verified >= target_end:
        return "Complete through target"

    progress = str(row.get("Scraping progress") or "").lower()
    if any(token in progress for token in ("running", "queued", "in progress", "active")):
        return "In progress"
    if any(
        token in progress
        for token in (
            "blocked",
            "permission",
            "licensed feed",
            "suspended",
            "publisher 403",
        )
    ):
        return "Blocked"
    if verified:
        return "Verified through earlier boundary"
    # Route hints such as ``cloudscraper`` or a historical access note do not
    # prove that the new estate-wide pass inspected and blocked the source.
    # Keep untouched inventory rows in the explicit pending pool.
    if not inspection_addressed(row):
        return "Not inspected"
    combined = " ".join(
        str(row.get(key) or "")
        for key in ("Problems identified", "Needs revisit", "Next action")
    ).lower()
    if any(
        token in combined
        for token in (
            "blocked",
            "permission",
            "licensed feed",
            "suspended",
            "publisher 403",
            "cloudflare",
        )
    ):
        return "Blocked"
    return "Inspected; verification pending"


def build_first_pass_note(row: dict[str, Any], *, state: str) -> str:
    """Create the short local method/issue note used by both tracker views."""

    explicit = str(row.get("First-pass note") or "").strip()
    if explicit:
        return explicit
    method = str(row.get("Method") or "Method pending review").strip()
    problem = str(row.get("Problems identified") or "").strip()
    if state == "Not inspected":
        return f"Method: {method}. First-pass inspection pending."
    if problem:
        return f"Method: {method}. Issue: {problem}"
    return f"Method: {method}. No issue note recorded."


def add_first_pass_fields(
    row: dict[str, Any],
    *,
    recorded_notion_boundary: date | None = None,
    target_end: date = FIRST_PASS_TARGET_END,
) -> None:
    """Add first-pass fields to one canonical row in place.

    Existing fields and statuses are preserved.  Verification comes only from
    an explicit first-pass value or the pre-existing strict/receipt field.
    """

    notion_boundary = recorded_notion_boundary or parse_date(
        row.get("Recorded Notion boundary")
    )
    notion_boundary = notion_boundary or parse_date(row.get("Required from"))
    verified = parse_date(row.get("First-pass verified through"))
    verified = verified or parse_date(row.get("Strict verified through"))
    verified = verified or parse_date(row.get("Coverage through"))

    explicit_partial = parse_date(row.get("Current-month partial through"))
    if explicit_partial and (
        verified is None
        or explicit_partial < target_end + timedelta(days=1)
        or explicit_partial > verified
    ):
        explicit_partial = None
    current_partial = explicit_partial
    if current_partial is None and verified and verified > target_end:
        current_partial = verified

    state = derive_first_pass_state(
        row, verified_through=verified, target_end=target_end
    )
    row["Recorded Notion boundary"] = (
        notion_boundary.isoformat() if notion_boundary else ""
    )
    row["First-pass target"] = target_end.isoformat()
    row["First-pass verified through"] = verified.isoformat() if verified else ""
    row["Current-month partial through"] = (
        current_partial.isoformat() if current_partial else ""
    )
    row["First-pass state"] = state
    row["First-pass note"] = build_first_pass_note(row, state=state)
