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
    "buys_today": 0,
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
    buys_today = sum(
        1
        for o in orders
        if _val(o.side) == "buy"
        and o.submitted_at is not None
        and o.submitted_at.astimezone(ET) >= midnight
    )
    return {
        "equity": float(acct.equity),
        "cash": float(acct.cash),
        "positions": positions,
        "open_order_symbols": open_order_symbols,
        "buys_today": buys_today,
    }


def submit_bracket(tc, order: dict) -> str:
    """Entry limit + stop + target as one atomic bracket. Returns order id."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    req = LimitOrderRequest(
        symbol=order["symbol"],
        qty=order["qty"],
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=order["limit_price"],
        order_class=OrderClass.BRACKET,
        stop_loss=StopLossRequest(stop_price=order["stop"]),
        take_profit=TakeProfitRequest(limit_price=order["target"]),
    )
    result = tc.submit_order(req)
    return str(result.id)


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
        live = [
            o for o in orders
            if o.symbol == symbol and _val(o.status) in ACTIVE_STATUSES
        ]
        entry_rec = journal.last_order_for(symbol)

        # --- time stop ---------------------------------------------------
        if entry_rec and entry_rec.get("signal_date"):
            held = _trading_days_held(
                date.fromisoformat(entry_rec["signal_date"]), now_et.date()
            )
            if held >= config.MAX_HOLD_DAYS:
                for o in live:
                    try:
                        tc.cancel_order_by_id(str(o.id))
                    except Exception as exc:  # noqa: BLE001
                        print(f"[exit] cancel {symbol} {o.id}: {exc}")
                tc.close_position(symbol)
                actions.append(
                    f"TIME EXIT {symbol}: held {held} trading days "
                    f"(max {config.MAX_HOLD_DAYS}), closed at market"
                )
                continue
        else:
            actions.append(f"WARNING {symbol}: no journal entry — age unknown, time stop skipped")

        # --- stop audit ---------------------------------------------------
        has_stop = any(
            _val(o.side) == "sell" and "stop" in _val(o.type or o.order_type)
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
                        qty=int(float(p.qty)),
                        side=OrderSide.SELL,
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
