"""Trading bot entrypoint, fired by the GitHub Actions scheduler.

Two modes, matching the two daily runs:
  entry -- morning run (~10:00 ET): fetch data, run rules, veto layer, place orders
  exit  -- pre-close run (15:30 ET): fully mechanical exit management, no AI

The scheduler fires four cron jobs (two per mode, to cover both US daylight
saving regimes), so on any given day each mode is triggered twice. The guard
below checks the actual New York clock and lets exactly one firing through;
the off-season duplicate lands outside its time window and exits cleanly.
"""

import argparse
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")

# The entry cron is aimed at 10:00 ET. GitHub cron can fire a few minutes
# late, so accept a window around the target rather than an exact time.
ENTRY_WINDOW = (time(9, 45), time(10, 45))

# The exit run must land while the market is still open, so its window is
# computed from the real close time (handles 13:00 half-day closes too).
EXIT_LEAD = timedelta(minutes=45)


def market_hours_today(now_et: datetime):
    """Return (open, close) datetimes for today's NYSE session, or None
    if the market is closed (weekend or holiday)."""
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=now_et.date(), end_date=now_et.date())
    if sched.empty:
        return None
    row = sched.iloc[0]
    return (
        row["market_open"].tz_convert(ET),
        row["market_close"].tz_convert(ET),
    )


def guard(mode: str, now_et: datetime) -> tuple[bool, str]:
    """Decide whether this firing should actually do work.

    Returns (ok, reason). A False here is normal and expected — it is how
    holidays and the daylight-saving duplicate firings get filtered out.
    """
    hours = market_hours_today(now_et)
    if hours is None:
        return False, "market closed today (weekend or NYSE holiday)"
    market_open, market_close = hours

    if mode == "entry":
        lo, hi = ENTRY_WINDOW
        if not (lo <= now_et.time() <= hi):
            return False, (
                f"outside entry window {lo}-{hi} ET "
                "(this is the duplicate DST cron — nothing wrong)"
            )
    elif mode == "exit":
        if now_et >= market_close:
            return False, f"market already closed at {market_close:%H:%M} ET"
        if now_et < market_close - EXIT_LEAD:
            return False, (
                f"too early — exit run wants the last {EXIT_LEAD} before the "
                f"{market_close:%H:%M} ET close (duplicate DST cron)"
            )
    else:
        return False, f"unknown mode {mode!r}"

    return True, "in window on a trading day"


def run_entry(now_et: datetime) -> None:
    """Morning run: steps 2-6 of the pipeline plug in here."""
    print(f"[entry] {now_et:%Y-%m-%d %H:%M} ET — running entry pipeline")

    # Step 2: assemble the daily data snapshot
    import data_fetch
    import rules

    snapshot = data_fetch.build_snapshot()
    path = data_fetch.save_snapshot(snapshot)
    print(data_fetch.summarize(snapshot))
    print(f"[entry] snapshot written to {path}")

    # Step 3: deterministic entry rules propose candidates (often none)
    candidates = rules.generate_candidates(snapshot)
    print(rules.explain(candidates))

    # Step 4: approved = claude_veto(candidates, snapshot)
    # Step 5: approved = apply_guardrails(approved, portfolio)
    # Step 6: place_bracket_orders(approved); notify_telegram(...); log(...)


def run_exit(now_et: datetime) -> None:
    """Pre-close run: purely mechanical, no AI. Bracket orders already carry
    stop and target, so this run only handles time-based exits (e.g. close
    positions older than N days) and sanity-checks that every open position
    still has a protective stop attached."""
    print(f"[exit] {now_et:%Y-%m-%d %H:%M} ET — running exit checks")
    print("[exit] pipeline stubs — nothing to do yet")


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading bot scheduled runner")
    parser.add_argument("--mode", required=True, choices=["entry", "exit"])
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the trading-day/time-window guard (manual testing)",
    )
    args = parser.parse_args()

    now_et = datetime.now(tz=ET)

    if args.force:
        print(f"[guard] bypassed via --force")
    else:
        ok, reason = guard(args.mode, now_et)
        print(f"[guard] mode={args.mode} now={now_et:%Y-%m-%d %H:%M %Z}: {reason}")
        if not ok:
            return 0  # clean exit — skipping is normal, not an error

    if args.mode == "entry":
        run_entry(now_et)
    else:
        run_exit(now_et)
    return 0


if __name__ == "__main__":
    sys.exit(main())
