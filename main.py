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
    """Morning run: snapshot -> rules -> (veto, step 4 TBD) -> guardrails ->
    bracket orders -> Telegram + journal."""
    print(f"[entry] {now_et:%Y-%m-%d %H:%M} ET — running entry pipeline")

    import broker
    import data_fetch
    import guardrails
    import journal
    import notify
    import rules

    # Step 2: assemble the daily data snapshot
    snapshot = data_fetch.build_snapshot()
    path = data_fetch.save_snapshot(snapshot)
    print(data_fetch.summarize(snapshot))
    print(f"[entry] snapshot written to {path}")

    # Step 3: deterministic entry rules propose candidates (often none)
    candidates = rules.generate_candidates(snapshot)
    print(rules.explain(candidates))

    # Step 4 (not built yet): Claude veto — may only shrink this list.

    # Step 5: hard guardrails size and cap whatever survived
    tc = broker.client()
    dry_run = tc is None
    account = dict(broker.SIM_ACCOUNT) if dry_run else broker.account_state(tc)
    approved, rejected = guardrails.apply(candidates, account)

    # Step 6: execution — bracket orders so exits exist from birth
    placed = []
    for order in approved:
        record = {
            "symbol": order["symbol"],
            "qty": order["qty"],
            "limit_price": order["limit_price"],
            "stop": order["stop"],
            "target": order["target"],
            "signal_date": order["signal_date"],
        }
        if dry_run:
            journal.log("order_dry_run", **record)
            placed.append(order)
            print(f"[entry] DRY RUN — would submit: {record}")
        else:
            try:
                order_id = broker.submit_bracket(tc, order)
                journal.log("order_submitted", order_id=order_id, **record)
                placed.append(order)
            except Exception as exc:  # noqa: BLE001 — a failed order is a missed trade, not a crash
                journal.log("order_error", symbol=order["symbol"], error=str(exc))
                rejected.append({"symbol": order["symbol"], "reason": f"submit failed: {exc}"})

    journal.log(
        "entry_run",
        date=snapshot["date"],
        dry_run=dry_run,
        market=snapshot["market"],
        macro_events=snapshot["macro_events"],
        candidates=[c["symbol"] for c in candidates],
        placed=[o["symbol"] for o in placed],
        rejected=rejected,
    )
    notify.send(compose_entry_message(snapshot, candidates, placed, rejected, account, dry_run))


def compose_entry_message(snapshot, candidates, placed, rejected, account, dry_run) -> str:
    m = snapshot["market"]
    lines = [f"[BOT] entry run {snapshot['date']}" + (" (DRY RUN)" if dry_run else "")]
    if m:
        trend = "above" if m["above_trend"] else "BELOW"
        lines.append(f"{m['benchmark']} {m['close']}, {trend} 50d MA ({m['ma50']})")
    for e in snapshot["macro_events"]:
        lines.append(f"macro: {e['event']} in {e['days_away']}d")
    if not candidates:
        lines.append("no setups today")
    for o in placed:
        lines.append(
            f"BUY {o['qty']} {o['symbol']} @ <={o['limit_price']} "
            f"| stop {o['stop']} | target {o['target']} ({o['reason']})"
        )
    for r in rejected:
        lines.append(f"skipped {r['symbol']}: {r['reason']}")
    lines.append(
        f"portfolio: equity {account['equity']:,.0f} | cash {account['cash']:,.0f} "
        f"| {len(account['positions'])} position(s)"
    )
    return "\n".join(lines)


def run_exit(now_et: datetime) -> None:
    """Pre-close run: purely mechanical, no AI. Bracket orders already carry
    stop and target, so this run only enforces the time stop and audits that
    every open position still has a protective stop attached."""
    print(f"[exit] {now_et:%Y-%m-%d %H:%M} ET — running exit checks")

    import broker
    import journal
    import notify

    tc = broker.client()
    if tc is None:
        print("[exit] DRY RUN — no broker, nothing to check")
        journal.log("exit_run", dry_run=True, actions=[])
        return

    actions = broker.exit_checks(tc, now_et)
    for a in actions:
        print(f"[exit] {a}")
    account = broker.account_state(tc)
    journal.log("exit_run", dry_run=False, actions=actions)
    notify.send(
        f"[BOT] exit run {now_et:%Y-%m-%d}\n"
        + "\n".join(actions)
        + f"\nportfolio: equity {account['equity']:,.0f} | cash {account['cash']:,.0f} "
        f"| {len(account['positions'])} position(s)"
    )


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
