"""
history.py — remembers every opportunity we've seen, so we can:
  * DEDUPE across days (don't re-flag the same bid as new every morning),
  * flag which items are NEW since the last run (for the "new" badge + email), and
  * CARRY FORWARD still-open opportunities that today's fetch didn't return.

Why carry-forward exists
------------------------
docs/opportunities.json used to be a pure snapshot of whatever the sources
returned in that one run, so an opportunity vanished from the dashboard the
moment a source stopped listing it — even with weeks left on its deadline.
That happens constantly and for reasons that have nothing to do with the bid
being closed: SAM.gov only returns notices posted in the last few days, several
Socrata feeds truncate at their row limit, and any source that throws returns
zero records. Measured on real data: 113 of 202 remembered opportunities were
missing from the dashboard, 53 of them still open.

So we store the FULL record here, not just a date, and the pipeline re-adds any
stored opportunity the sources didn't return. An item now leaves the dashboard
for exactly one reason: its due date passed (see pipeline.is_active).

Storage is a single committed JSON file (data/history.json). No database.
Shape:
    { "<dedupe_key>": {
        "first_seen": "YYYY-MM-DD",   # first time we ever saw it
        "last_seen":  "YYYY-MM-DD",   # last time a source actually returned it
        "record":     { ...full Opportunity fields... }
      }, ... }

Legacy entries written before carry-forward have only "first_seen" and no
"record". They still load and still suppress the "new" badge correctly; they
just can't be revived, because their data was never saved. No migration needed.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_HISTORY_PATH = os.path.normpath(os.path.join(_HERE, "..", "data", "history.json"))


def load_history(path: str | None = None) -> dict:
    path = path or _DEFAULT_HISTORY_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        # A corrupt history file should never crash the run — start fresh.
        return {}
    # Tolerate anything that isn't the shape we expect rather than crashing.
    return data if isinstance(data, dict) else {}


def save_history(history: dict, path: str | None = None, retention_days: int = 180) -> None:
    path = path or _DEFAULT_HISTORY_PATH
    pruned = _prune(history, retention_days)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2, sort_keys=True)


def _prune(history: dict, retention_days: int) -> dict:
    """Drop entries we no longer need.

    An entry is kept if EITHER:
      * its deadline hasn't passed (still biddable — we must keep the record so
        carry-forward can re-add it, however long ago we first saw it), or
      * we first saw it within retention_days (recent enough to keep suppressing
        a false "new" badge if it comes back).

    The still-open test is what stops retention_days from silently deleting a
    live opportunity: a bid first seen today with a 12-month deadline would
    otherwise be pruned at day 180 and pop back up as "new".
    """
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=retention_days)).isoformat()
    kept = {}
    for key, entry in history.items():
        if not isinstance(entry, dict):
            continue
        if _still_open(entry, today):
            kept[key] = entry
        elif entry.get("first_seen", "9999-99-99") >= cutoff:
            kept[key] = entry
    return kept


def _still_open(entry: dict, today: date) -> bool:
    """True if the stored record has a due date that hasn't passed yet."""
    due = ((entry.get("record") or {}).get("due_date") or "")[:10]
    if len(due) < 10:
        return False
    try:
        return date.fromisoformat(due) >= today
    except ValueError:
        return False
