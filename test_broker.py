"""Tests for the mechanical exit pass, using a fake trading client.

The exit run is the only code that closes positions, so its decisions are
worth pinning down: the time stop, the earnings exit, and the stop audit —
including that each one cancels resting bracket legs before flattening.

Run:  python test_broker.py
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import broker
import config
import journal

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 5, 14, 15, tzinfo=ET)


class Position:
    def __init__(self, symbol, qty):
        self.symbol, self.qty = symbol, str(qty)


class Order:
    def __init__(self, symbol, order_id="o1", side="sell", type_="stop",
                 status="new"):
        self.symbol, self.id, self.side = symbol, order_id, side
        self.type, self.order_type, self.status = type_, type_, status
        self.legs = []


class FakeClient:
    """Records what the exit pass did to it."""

    def __init__(self, positions, orders=()):
        self._positions = list(positions)
        self._orders = list(orders)
        self.cancelled, self.closed, self.submitted = [], [], []

    def get_all_positions(self):
        return self._positions

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)

    def close_position(self, symbol):
        self.closed.append(symbol)

    def submit_order(self, req):
        self.submitted.append(req)
        return req


def setup(entry_days_ago=1, earnings_in=None, orders=()):
    """Patch the pass's three outside dependencies and return a fake client."""
    broker._recent_orders = lambda tc, days=30: list(orders)
    broker.trading_days_to_earnings = lambda symbol, today: earnings_in
    # An entry recent enough that the time stop is not what fires.
    signal = "2026-08-04" if entry_days_ago <= 1 else "2026-07-20"
    journal.last_order_for = lambda symbol: {"signal_date": signal, "stop": 100.0}


def test_earnings_exit_closes_the_position():
    setup(earnings_in=1)
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == ["ANET"], actions
    assert any("EARNINGS EXIT ANET" in a and "1 session" in a for a in actions), actions


def test_earnings_exit_respects_the_threshold():
    setup(earnings_in=config.EXIT_BEFORE_EARNINGS_DAYS)
    tc = FakeClient([Position("ANET", 51)])
    broker.exit_checks(tc, NOW)
    assert tc.closed == ["ANET"], "at the threshold it must close"

    setup(earnings_in=config.EXIT_BEFORE_EARNINGS_DAYS + 1)
    tc = FakeClient([Position("ANET", 51)])
    broker.exit_checks(tc, NOW)
    assert tc.closed == [], "one session beyond the threshold must be held"


def test_unknown_earnings_holds_rather_than_flattening():
    # A yfinance hiccup must not liquidate a working position.
    setup(earnings_in=None)
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == []
    assert any("earnings date unknown" in a and "holding" in a for a in actions), actions


def test_earnings_exit_cancels_bracket_legs_first():
    # Flattening while a stop and target still rest would leave naked orders.
    legs = [Order("ANET", "stop-1", side="sell", type_="stop"),
            Order("ANET", "tgt-1", side="sell", type_="limit")]
    setup(earnings_in=0, orders=legs)
    tc = FakeClient([Position("ANET", 51)])
    broker.exit_checks(tc, NOW)
    assert sorted(tc.cancelled) == ["stop-1", "tgt-1"]
    assert tc.closed == ["ANET"]


def test_earnings_exit_works_for_a_short():
    setup(earnings_in=1)
    tc = FakeClient([Position("TSM", -12)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == ["TSM"]
    assert any("(short)" in a for a in actions), actions


def test_time_stop_still_takes_precedence():
    setup(entry_days_ago=10, earnings_in=30)
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == ["ANET"]
    assert any("TIME EXIT" in a for a in actions), actions


def test_earnings_exit_can_be_switched_off():
    original = config.EXIT_BEFORE_EARNINGS
    config.EXIT_BEFORE_EARNINGS = False
    try:
        setup(earnings_in=0)
        tc = FakeClient([Position("ANET", 51)])
        actions = broker.exit_checks(tc, NOW)
        assert tc.closed == []
        assert not any("EARNINGS" in a for a in actions), actions
    finally:
        config.EXIT_BEFORE_EARNINGS = original


def test_no_positions_is_a_clean_no_op():
    setup()
    assert broker.exit_checks(FakeClient([]), NOW) == ["no open positions"]


def test_stop_audit_reattaches_when_protection_is_missing():
    # No resting orders at all: the audit must put a stop back on.
    setup(earnings_in=30)
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == []
    assert len(tc.submitted) == 1
    assert any("STOP RE-ATTACHED" in a for a in actions), actions


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
