"""Decision journal: append-only JSONL, one record per decision or event.

This file is the experiment's memory. Every run appends what it saw and did
(snapshot summary, candidates, guardrail rejections, orders, exits), which
is what makes the monthly review and the A/B/C arm comparison possible.
The GitHub Actions workflow commits it back to the repo after each run, so
nothing is lost when the ephemeral runner dies.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

PATH = Path(__file__).parent / "journal.jsonl"


def log(event: str, **data) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **data,
    }
    with PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def entries() -> list[dict]:
    if not PATH.exists():
        return []
    return [
        json.loads(line)
        for line in PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def already_ran(event: str, date_iso: str) -> bool:
    """True if a real (non-dry-run) record of this event exists for this date.

    This is the dedupe that lets the scheduler's time windows be generous:
    with four cron firings a day and GitHub's unpredictable delays, several
    firings can land inside a valid window — the first one to complete wins,
    and the rest see its journal record and skip.
    """
    return any(
        rec.get("event") == event
        and rec.get("date") == date_iso
        and not rec.get("dry_run", False)
        for rec in entries()
    )


def last_order_for(symbol: str) -> dict | None:
    """Most recent entry order record for a symbol (used by the exit run to
    determine position age and the original stop price)."""
    for record in reversed(entries()):
        if record["event"] in ("order_submitted", "order_dry_run") and record.get("symbol") == symbol:
            return record
    return None
