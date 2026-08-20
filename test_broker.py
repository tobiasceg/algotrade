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

# The retry/settle loops sleep between attempts; nothing here needs real
# waiting, and the fake client settles cancels instantly.
broker.time_mod.sleep = lambda seconds: None


class Position:
    def __init__(self, symbol, qty, avg_entry_price=194.10, current_price=195.51,
                 unrealized_plpc=0.00727):
        self.symbol, self.qty = symbol, str(qty)
        self.avg_entry_price = str(avg_entry_price)
        self.current_price = str(current_price)
        self.unrealized_plpc = str(unrealized_plpc)


class Order:
    def __init__(self, symbol, order_id="o1", side="sell", type_="stop",
                 status="new", qty=51, legs=None):
        self.symbol, self.id, self.side = symbol, order_id, side
        self.type, self.order_type, self.status = type_, type_, status
        self.qty = str(qty)
        self.legs = legs if legs is not None else []


def protection(symbol, qty=51, side="sell"):
    """A healthy pair of resting exits: stop plus target."""
    return [
        Order(symbol, "stop-1", side=side, type_="stop", qty=qty),
        Order(symbol, "tgt-1", side=side, type_="limit", qty=qty),
    ]


class FakeClient:
    """Records what the exit pass did to it."""

    def __init__(self, positions, orders=()):
        self._positions = list(positions)
        self._orders = list(orders)
        self.cancelled, self.closed, self.submitted, self.rejected = [], [], [], []

    def get_all_positions(self):
        return self._positions

    cancel_fails = ()

    def cancel_order_by_id(self, order_id):
        if order_id in self.cancel_fails:
            raise RuntimeError(f"cannot cancel {order_id}")
        self.cancelled.append(order_id)

    def close_position(self, symbol):
        self.closed.append(symbol)

    # Number of leading submit_order calls to reject, and with what.
    reject_submits = 0
    reject_error = "insufficient qty available for order (requested: 163, available: 0)"

    def submit_order(self, req):
        if self.reject_submits > 0:
            self.reject_submits -= 1
            self.rejected.append(req)
            raise RuntimeError(self.reject_error)
        self.submitted.append(req)
        return req

    def get_order_by_id(self, order_id):
        return Order("X", order_id, status="canceled")


def setup(entry_days_ago=1, earnings_in=None, orders=()):
    """Patch the pass's three outside dependencies and return a fake client."""
    broker._recent_orders = lambda tc, days=30: list(orders)
    broker.trading_days_to_earnings = lambda symbol, today: earnings_in
    # An entry recent enough that the time stop is not what fires.
    signal = "2026-08-04" if entry_days_ago <= 1 else "2026-07-20"
    journal.last_order_for = lambda symbol: {
        "signal_date": signal, "stop": 100.0, "target": 220.0,
    }


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


def test_healthy_position_reports_a_status_line():
    # A silent exit run reads the same whether a position is fine or was
    # never checked. The Aug 5 run logged an empty action list while holding
    # ANET; it must now say what it found.
    setup(earnings_in=63, orders=protection("ANET"))
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == [] and actions != []
    line = actions[0]
    assert "ANET: holding" in line, line
    assert "day 1 of 5" in line, line
    assert "+0.7%" in line, line
    assert "194.10 -> 195.51" in line, line
    assert "earnings in 63" in line, line
    assert "stop+target ok" in line, line


def test_status_line_marks_a_short():
    setup(earnings_in=40, orders=protection("TSM", qty=12, side="buy"))
    tc = FakeClient([Position("TSM", -12)])
    line = broker.exit_checks(tc, NOW)[0]
    assert "TSM (short): holding" in line, line
    assert "day 1 of 3" in line, line   # shorts use the shorter leash


def test_status_line_survives_missing_price_fields():
    class Bare:
        symbol, qty = "ANET", "51"

    setup(earnings_in=10, orders=protection("ANET"))
    line = broker.exit_checks(FakeClient([Bare()]), NOW)[0]
    assert "ANET: holding" in line and "stop+target ok" in line, line


def test_no_status_line_when_an_exit_fired():
    setup(earnings_in=1)
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert all("holding" not in a for a in actions), actions


def test_audit_reattaches_when_all_protection_is_missing():
    # The Aug 17 case: legs expired overnight, nothing resting at all.
    setup(earnings_in=30)
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.closed == []
    assert len(tc.submitted) == 1, tc.submitted
    assert any("PROTECTION RE-ATTACHED" in a for a in actions), actions


def test_audit_restores_a_missing_target_not_just_the_stop():
    # A lone stop is where every position ended up previously: the 1.5:1
    # reward:risk does not exist without the target. The stale stop must be
    # cleared first so the replacement OCO does not stack on top of it.
    setup(earnings_in=30, orders=[Order("ANET", "stop-1", type_="stop")])
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.cancelled == ["stop-1"], tc.cancelled
    assert len(tc.submitted) == 1, tc.submitted
    assert any("target missing" in a for a in actions), actions


def test_restored_protection_is_one_oco_not_two_loose_orders():
    # Two independent orders would leave an orphan after the first fills,
    # and that orphan would open a position in the opposite direction.
    setup(earnings_in=30)
    tc = FakeClient([Position("ANET", 51)])
    broker.exit_checks(tc, NOW)
    (req,) = tc.submitted
    assert str(getattr(req, "order_class", "")).lower().endswith("oco"), req
    assert req.stop_loss is not None and req.take_profit is not None


def test_audit_never_stacks_when_the_stale_order_cannot_be_cleared():
    # If the old stop survives and we attach anyway, the position carries two
    # stops: the second fill does not exit it, it opens a short.
    setup(earnings_in=30, orders=[Order("ANET", "stop-1", type_="stop")])
    tc = FakeClient([Position("ANET", 51)])
    tc.cancel_fails = ("stop-1",)
    actions = broker.exit_checks(tc, NOW)
    assert tc.submitted == [], "must not add protection on top of a stuck order"
    assert any("could not clear resting order" in a for a in actions), actions


def test_oco_rejection_falls_back_to_a_bare_stop():
    # The live Aug 18 HPE failure: the stop was cancelled, the OCO bounced on
    # "insufficient qty" while the cancel settled, and the position was left
    # naked overnight. A rejected pair must never mean no protection.
    setup(earnings_in=30, orders=[Order("HPE", "stop-1", type_="stop", qty=163)])
    tc = FakeClient([Position("HPE", 163)])
    tc.reject_submits = 3                        # every OCO attempt fails
    actions = broker.exit_checks(tc, NOW)
    assert tc.cancelled == ["stop-1"]
    assert len(tc.submitted) == 1, tc.submitted  # the bare stop got on
    assert any("fell back to a bare stop" in a for a in actions), actions
    assert not any("UNPROTECTED" in a for a in actions), actions


def test_oco_succeeds_on_a_retry_after_the_cancel_settles():
    setup(earnings_in=30, orders=[Order("HPE", "stop-1", type_="stop", qty=163)])
    tc = FakeClient([Position("HPE", 163)])
    tc.reject_submits = 1                        # first attempt only
    actions = broker.exit_checks(tc, NOW)
    assert len(tc.submitted) == 1
    assert any("PROTECTION RE-ATTACHED" in a for a in actions), actions


def test_total_failure_is_flagged_as_unprotected():
    setup(earnings_in=30, orders=[Order("HPE", "stop-1", type_="stop", qty=163)])
    tc = FakeClient([Position("HPE", 163)])
    tc.reject_submits = 99                       # nothing will go on
    actions = broker.exit_checks(tc, NOW)
    assert tc.submitted == []
    assert any("UNPROTECTED" in a for a in actions), actions


def test_unfilled_gtc_entry_is_cancelled():
    # Entries are GTC now; one that never filled must not survive the day.
    parent = Order("NVDA", "entry-1", side="buy", type_="limit",
                   legs=[Order("NVDA", "leg-1")])
    setup(earnings_in=30, orders=protection("ANET") + [parent])
    tc = FakeClient([Position("ANET", 51)])
    actions = broker.exit_checks(tc, NOW)
    assert tc.cancelled == ["entry-1"], tc.cancelled
    assert any("UNFILLED ENTRY CANCELLED NVDA" in a for a in actions), actions


def test_filled_entry_legs_are_never_cancelled():
    # Once filled, the symbol is held and those legs ARE the protection.
    parent = Order("ANET", "entry-1", side="buy", type_="limit",
                   legs=[Order("ANET", "leg-1")])
    setup(earnings_in=30, orders=protection("ANET") + [parent])
    tc = FakeClient([Position("ANET", 51)])
    broker.exit_checks(tc, NOW)
    assert tc.cancelled == [], "must not cancel protection on a held position"


def test_unfilled_entry_is_reaped_even_with_no_positions():
    parent = Order("NVDA", "entry-1", side="buy", type_="limit",
                   legs=[Order("NVDA", "leg-1")])
    setup(orders=[parent])
    tc = FakeClient([])
    actions = broker.exit_checks(tc, NOW)
    assert tc.cancelled == ["entry-1"]
    assert any("UNFILLED ENTRY CANCELLED" in a for a in actions), actions


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
