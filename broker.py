"""Step 6, broker side: Alpaca paper trading.

Entries go in as bracket orders — entry limit + stop-loss + take-profit in
one atomic order — so every position is born with its exits attached and
stays protected even if every later scheduled run fails.

The pre-close exit run is fully mechanical (no AI anywhere near it):
  1. time stop — positions held >= MAX_HOLD_DAYS trading days get closed;
  2. stop audit — any position missing an active stop order gets one
     re-attached from the journal, or loudly flagged if that's impossible.

If ALPACA_API_KEY / ALPACA_SECRET_KEY are unset, client() returns None and
callers fall back to SIM_ACCOUNT for a dry run.
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

import config
import journal

ET = ZoneInfo("America/New_York")

# Fake account used for dry runs (no API keys) and tests.
SIM_ACCOUNT = {
    "equity": 100_000.0,
    "cash": 100_000.0,
    "positions": {},
    "open_order_symbols": [],
    "entries_today": 0,
}

# Alpaca order states that mean "this order is still live".
ACTIVE_STATUSES = {
    "new", "accepted", "held", "partially_filled",
    "pending_new", "accepted_for_bidding", "calculated",
}


def _val(x) -> str:
    """Enum-or-string -> plain string (alpaca-py mixes both across versions)."""
    return str(getattr(x, "value", x))


def client():
    """TradingClient if keys are configured, else None (dry-run mode).

    paper=True is hardcoded on purpose. Going live should require editing
    this line with your eyes open, not flipping an env var by accident.
    """
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print("[broker] no Alpaca keys in env — DRY RUN mode")
        return None
    from alpaca.trading.client import TradingClient

    return TradingClient(key, secret, paper=True)


def _recent_orders(tc, days: int = 30) -> list:
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    req = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=datetime.now(tz=ET) - timedelta(days=days),
        limit=500,
        nested=True,
    )
    orders = tc.get_orders(filter=req)
    # Flatten bracket children so stop legs are visible alongside parents.
    flat = []
    for o in orders:
        flat.append(o)
        flat.extend(o.legs or [])
    return flat


def account_state(tc) -> dict:
    """Live account in the same shape guardrails.apply() expects."""
    acct = tc.get_account()
    positions = {
        p.symbol: {
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
        }
        for p in tc.get_all_positions()
    }
    orders = _recent_orders(tc)
    midnight = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0)
    open_order_symbols = sorted(
        {o.symbol for o in orders if _val(o.status) in ACTIVE_STATUSES}
    )
    # Count entries placed today whether or not they filled — this is what
    # makes the 2-trades-per-day cap robust against a duplicate morning run.
    # Every bot entry (long BUY or short SELL) is a bracket parent, which is
    # the only order kind in the flattened list that carries child legs.
    entries_today = sum(
        1
        for o in orders
        if getattr(o, "legs", None)
        and o.submitted_at is not None
        and o.submitted_at.astimezone(ET) >= midnight
    )
    return {
        "equity": float(acct.equity),
        "cash": float(acct.cash),
        "positions": positions,
        "open_order_symbols": open_order_symbols,
        "entries_today": entries_today,
    }


def submit_bracket(tc, order: dict) -> str:
    """Entry limit + stop + target as one atomic bracket. Returns order id.

    Works for both books: a long entry is a BUY with the stop below and
    target above; a short entry is a SELL with the stop above and target
    below. Alpaca's bracket class handles both natively.
    """
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    side = OrderSide.SELL if order.get("side") == "short" else OrderSide.BUY
    req = LimitOrderRequest(
        symbol=order["symbol"],
        qty=order["qty"],
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=order["limit_price"],
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=order["stop"]),
        take_profit=TakeProfitRequest(limit_price=order["target"]),
    )
    result = tc.submit_order(req)
    return str(result.id)


def shortable(tc, symbol: str) -> bool:
    """True only if Alpaca marks the asset shortable AND easy to borrow.
    Any lookup failure fails closed — a missed short is cheap."""
    try:
        asset = tc.get_asset(symbol)
        return bool(asset.shortable) and bool(getattr(asset, "easy_to_borrow", False))
    except Exception as exc:  # noqa: BLE001 — fail closed on any API problem
        print(f"[broker] shortable({symbol}) lookup failed: {exc}")
        return False


def trading_days_to_earnings(symbol: str, today: date) -> int | None:
    """Sessions until this symbol's next scheduled report, or None if unknown.

    Fetched fresh rather than read from the entry snapshot: the exit run is a
    separate CI job on a clean runner (no snapshot file), and an earnings date
    can be scheduled after the position was opened. Only ever called for
    symbols actually held, so it is a handful of calls at most.
    """
    try:
        import yfinance as yf

        import data_fetch

        cal = yf.Ticker(symbol).calendar or {}
        dates = cal.get("Earnings Date") or []
        future = sorted(d for d in dates if d >= today)
        if not future:
            return None
        return data_fetch.trading_days_until(
            future[0], data_fetch.upcoming_sessions(today)
        )
    except Exception as exc:  # noqa: BLE001 — a scrape failure must not break exits
        print(f"[exit] earnings lookup failed for {symbol}: {exc}")
        return None


def _trading_days_held(entry_date: date, today: date) -> int:
    sched = mcal.get_calendar("NYSE").schedule(start_date=entry_date, end_date=today)
    return max(len(sched) - 1, 0)


def exit_checks(tc, now_et: datetime) -> list[str]:
    """Mechanical pre-close pass. Returns human-readable action lines."""
    actions: list[str] = []
    positions = tc.get_all_positions()
    if not positions:
        return ["no open positions"]

    orders = _recent_orders(tc)

    for p in positions:
        symbol = p.symbol
        is_short = float(p.qty) < 0
        live = [
            o for o in orders
            if o.symbol == symbol and _val(o.status) in ACTIVE_STATUSES
        ]
        entry_rec = journal.last_order_for(symbol)

        def close_out(reason: str) -> None:
            """Cancel any resting bracket legs, then flatten at market."""
            for o in live:
                try:
                    tc.cancel_order_by_id(str(o.id))
                except Exception as exc:  # noqa: BLE001
                    print(f"[exit] cancel {symbol} {o.id}: {exc}")
            tc.close_position(symbol)
            actions.append(reason)

        # --- time stop ---------------------------------------------------
        # Shorts get a shorter leash: bear-market rallies are violent.
        max_hold = config.SHORT_MAX_HOLD_DAYS if is_short else config.MAX_HOLD_DAYS
        if entry_rec and entry_rec.get("signal_date"):
            held = _trading_days_held(
                date.fromisoformat(entry_rec["signal_date"]), now_et.date()
            )
            if held >= max_hold:
                close_out(
                    f"TIME EXIT {symbol}{' (short)' if is_short else ''}: "
                    f"held {held} trading days (max {max_hold}), closed at market"
                )
                continue
        else:
            actions.append(f"WARNING {symbol}: no journal entry — age unknown, time stop skipped")

        # --- earnings exit -------------------------------------------------
        # The entry gates stop us opening near a report, but a hold can still
        # run into one. Being flat is the only defence against a gap, which
        # opens through a stop rather than at it.
        if config.EXIT_BEFORE_EARNINGS:
            ted = trading_days_to_earnings(symbol, now_et.date())
            if ted is None:
                # Deliberately do NOT flatten on an unknown date: closing a
                # working position every time a scrape hiccups is worse than
                # the residual risk the entry gate already screens for.
                actions.append(
                    f"WARNING {symbol}: earnings date unknown — cannot verify, holding"
                )
            elif ted <= config.EXIT_BEFORE_EARNINGS_DAYS:
                close_out(
                    f"EARNINGS EXIT {symbol}{' (short)' if is_short else ''}: "
                    f"reports in {ted} session(s), closed at market"
                )
                continue

        # --- stop audit ---------------------------------------------------
        # A long is protected by a SELL stop below; a short by a BUY stop above.
        protect_side = "buy" if is_short else "sell"
        has_stop = any(
            _val(o.side) == protect_side and "stop" in _val(o.type or o.order_type)
            for o in live
        )
        if not has_stop:
            stop_price = (entry_rec or {}).get("stop")
            if stop_price:
                from alpaca.trading.enums import OrderSide, TimeInForce
                from alpaca.trading.requests import StopOrderRequest

                tc.submit_order(
                    StopOrderRequest(
                        symbol=symbol,
                        qty=abs(int(float(p.qty))),
                        side=OrderSide.BUY if is_short else OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        stop_price=stop_price,
                    )
                )
                actions.append(f"STOP RE-ATTACHED {symbol} @ {stop_price} (was unprotected!)")
            else:
                actions.append(
                    f"ALERT {symbol}: NO STOP and none in journal — needs manual attention"
                )

    return actions
