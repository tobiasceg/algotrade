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
import os
import sys
import time as time_mod
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")

# The entry cron is aimed at 10:00 ET, but GitHub Actions cron routinely
# fires 1-2+ hours late. The window is therefore generous: any firing that
# lands before mid-afternoon still makes a valid entry decision (the DAY
# limit order at signal_close * 1.02 means a late entry can never chase a
# runaway price — it just doesn't fill). Duplicate firings inside the
# window are handled by the journal dedupe, not by window narrowness.
ENTRY_WINDOW = (time(9, 45), time(13, 30))

# The exit run must land while the market is still open, so its window is
# computed from the real close time (handles 13:00 half-day closes too).
# Wide for the same reason: the cron aims early and may be hours late;
# anything from EXIT_LEAD before the close until the close works.
EXIT_LEAD = timedelta(hours=2, minutes=15)

# Beat GitHub's cron lag by aiming EARLY and sleeping to the target: the
# crons fire hours before the intended work time, and whatever queue delay
# GitHub adds simply eats into a harmless sleep instead of our punctuality.
ENTRY_TARGET = time(10, 0)                  # work starts 10:00 ET sharp
EXIT_TARGET_LEAD = timedelta(minutes=105)   # close - 1h45m = 14:15 ET on
                                            # normal days, 11:15 on half days
MAX_WAIT = timedelta(hours=5)               # sanity cap on any single sleep


def seconds_until_target(mode: str, now_et: datetime, market_close: datetime):
    """How long an early firing should sleep before doing its work.

    Returns (seconds, target). Zero seconds means work immediately —
    either we're already at/past the target (a delayed firing) or the
    gap exceeds MAX_WAIT (leave it to the guard to reject).
    """
    if mode == "entry":
        target = datetime.combine(now_et.date(), ENTRY_TARGET, tzinfo=ET)
    else:
        target = market_close - EXIT_TARGET_LEAD
    delta = target - now_et
    if timedelta(0) < delta <= MAX_WAIT:
        return delta.total_seconds(), target
    return 0.0, target


def cron_lag_minutes(cron: str, now_utc: datetime) -> float | None:
    """Minutes between a cron expression's scheduled slot and now.

    Crons here fire at most once a day, so the slot is today's HH:MM from
    the expression; a negative gap means the firing slipped past midnight
    UTC, so it belongs to yesterday's slot.
    """
    try:
        minute, hour = cron.split()[:2]
        scheduled = now_utc.replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0
        )
    except (ValueError, IndexError):
        return None
    lag = (now_utc - scheduled).total_seconds() / 60
    if lag < 0:
        lag += 24 * 60
    return round(lag, 1)


def log_timing_probe(mode: str) -> None:
    """On market-closed days, turn each firing into scheduler telemetry.

    No trading happens, but GitHub's cron lag is exactly as measurable on a
    Saturday as on a Monday — so closed days build the dataset that tells
    us whether the free scheduler is reliable enough to keep. Records go to
    the journal only (no Telegram; eight probes a day would be spam).
    """
    cron = os.environ.get("SCHEDULE_CRON", "")
    now_utc = datetime.now(tz=timezone.utc)
    lag = cron_lag_minutes(cron, now_utc)
    if lag is None:
        return  # manual or local run — nothing to measure

    import journal

    journal.log(
        "timing_probe",
        date=now_utc.date().isoformat(),
        mode=mode,
        cron=cron,
        scheduled_utc=cron.split()[1].zfill(2) + ":" + cron.split()[0].zfill(2),
        actual_utc=now_utc.strftime("%H:%M"),
        lag_minutes=lag,
    )
    print(f"[probe] market closed — {mode} cron '{cron}' ran {lag:.0f} min after its slot")


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

    # Dedupe: with generous windows, several of the day's cron firings can
    # be valid — the first one to complete journals its run, the rest skip.
    import journal

    today = now_et.date().isoformat()
    if journal.already_ran(f"{mode}_run", today):
        return False, f"{mode} run already completed today (duplicate firing)"

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
    import short_rules
    import veto

    # Step 2: assemble the daily data snapshot
    snapshot = data_fetch.build_snapshot()
    path = data_fetch.save_snapshot(snapshot)
    print(data_fetch.summarize(snapshot))
    print(f"[entry] snapshot written to {path}")

    # Step 3: deterministic entry rules propose candidates (often none).
    # If the long book produced nothing, consult the short book (3b) — its
    # regime gate (QQQ well below the 50d MA) is the mirror of the long
    # gate, so at most one book can ever be active on a given day.
    candidates = rules.generate_candidates(snapshot)
    print(rules.explain(candidates))
    book = "long"
    if not candidates:
        candidates = short_rules.generate_candidates(snapshot)
        print(short_rules.explain(candidates))
        if candidates:
            book = "short"

    # Step 4: Claude veto — may only shrink the list, never expand it
    survivors, veto_decisions = veto.review(candidates, snapshot)

    # Step 5: hard guardrails size and cap whatever survived
    tc = broker.client()
    dry_run = tc is None
    account = dict(broker.SIM_ACCOUNT) if dry_run else broker.account_state(tc)
    approved, rejected = guardrails.apply(survivors, account)

    # Step 6: execution — bracket orders so exits exist from birth
    placed = []
    for order in approved:
        record = {
            "symbol": order["symbol"],
            "side": order.get("side", "long"),
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
            # Borrowability is broker state, not rules state — checked here,
            # at the last moment before submission. Fails closed.
            if order.get("side") == "short" and not broker.shortable(tc, order["symbol"]):
                reason = "not shortable / not easy-to-borrow at Alpaca"
                journal.log("order_rejected", symbol=order["symbol"], reason=reason)
                rejected.append({"symbol": order["symbol"], "reason": reason})
                continue
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
        book=book,
        market=snapshot["market"],
        macro_events=snapshot["macro_events"],
        candidates=[c["symbol"] for c in candidates],
        veto=veto_decisions,
        placed=[o["symbol"] for o in placed],
        rejected=rejected,
    )
    notify.send(
        compose_entry_message(
            snapshot, candidates, veto_decisions, placed, rejected, account, dry_run
        )
    )


def compose_entry_message(snapshot, candidates, veto_decisions, placed, rejected, account, dry_run) -> str:
    m = snapshot["market"]
    lines = [f"[BOT] entry run {snapshot['date']}" + (" (DRY RUN)" if dry_run else "")]
    if m:
        trend = "above" if m["above_trend"] else "BELOW"
        lines.append(f"{m['benchmark']} {m['close']}, {trend} 50d MA ({m['ma50']})")
    for e in snapshot["macro_events"]:
        lines.append(f"macro: {e['event']} in {e['days_away']}d")
    if not candidates:
        lines.append("no setups today")
    for d in veto_decisions:
        if d["decision"] == "VETO":
            lines.append(f"VETOED {d['symbol']}: {d['reason']}")
        elif d["decision"] == "SKIPPED":
            lines.append(f"note: {d['reason']}")
    for o in placed:
        if o.get("side") == "short":
            lines.append(
                f"SHORT {o['qty']} {o['symbol']} @ >={o['limit_price']} "
                f"| stop {o['stop']} | target {o['target']} ({o['reason']})"
            )
        else:
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
        journal.log("exit_run", date=now_et.date().isoformat(), dry_run=True, actions=[])
        return

    actions = broker.exit_checks(tc, now_et)
    for a in actions:
        print(f"[exit] {a}")
    account = broker.account_state(tc)
    journal.log("exit_run", date=now_et.date().isoformat(), dry_run=False, actions=actions)
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
        # Cheap disqualifiers first, so an early firing never sleeps for
        # hours only to discover the day was a holiday or already handled.
        import journal

        if market_hours_today(now_et) is None:
            print(f"[guard] {now_et:%Y-%m-%d}: market closed (weekend or NYSE holiday)")
            log_timing_probe(args.mode)
            return 0
        if journal.already_ran(f"{args.mode}_run", now_et.date().isoformat()):
            print(f"[guard] {args.mode} run already completed today — skipping")
            return 0

        # Early firing? Sleep until the target work time (see workflow:
        # crons aim early so GitHub's queue lag lands inside this sleep).
        _, market_close = market_hours_today(now_et)
        wait_s, target = seconds_until_target(args.mode, now_et, market_close)
        if wait_s > 0:
            print(
                f"[wait] fired early at {now_et:%H:%M} ET — "
                f"sleeping {int(wait_s / 60)} min until {target:%H:%M} ET"
            )
            time_mod.sleep(wait_s)
            now_et = datetime.now(tz=ET)

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
