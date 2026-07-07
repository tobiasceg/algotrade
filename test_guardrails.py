"""Tests for the hard guardrails, using fake accounts.

Run:  python test_guardrails.py
"""

import guardrails


def make_candidate(symbol="VRT", close=100.0, **overrides) -> dict:
    base = {
        "symbol": symbol,
        "action": "BUY",
        "signal_date": "2026-07-06",
        "close": close,
        "stop": round(close * 0.95, 2),
        "target": round(close * 1.08, 2),
        "reason": "test",
    }
    base.update(overrides)
    return base


def make_account(**overrides) -> dict:
    base = {
        "equity": 100_000.0,
        "cash": 100_000.0,
        "positions": {},
        "open_order_symbols": [],
        "buys_today": 0,
    }
    base.update(overrides)
    return base


def test_sizing_respects_position_budget():
    approved, rejected = guardrails.apply([make_candidate(close=100.0)], make_account())
    assert rejected == []
    (order,) = approved
    assert order["limit_price"] == 102.0            # close * 1.02
    assert order["qty"] == 98                       # 10_000 budget // 102
    assert order["est_cost"] <= 10_000


def test_daily_trade_cap():
    cands = [make_candidate(s) for s in ("VRT", "ANET", "SMCI")]
    approved, rejected = guardrails.apply(cands, make_account())
    assert [o["symbol"] for o in approved] == ["VRT", "ANET"]  # first two = strongest
    assert rejected[0]["symbol"] == "SMCI" and "daily cap" in rejected[0]["reason"]


def test_cap_counts_earlier_buys_today():
    approved, rejected = guardrails.apply(
        [make_candidate()], make_account(buys_today=2)
    )
    assert approved == [] and "daily cap" in rejected[0]["reason"]


def test_whitelist_enforced():
    approved, rejected = guardrails.apply(
        [make_candidate(symbol="GME")], make_account()
    )
    assert approved == [] and "whitelist" in rejected[0]["reason"]


def test_no_doubling_into_existing_position():
    account = make_account(positions={"VRT": {"qty": 50}})
    approved, rejected = guardrails.apply([make_candidate("VRT")], account)
    assert approved == [] and "already holding" in rejected[0]["reason"]


def test_no_doubling_into_open_order():
    account = make_account(open_order_symbols=["VRT"])
    approved, rejected = guardrails.apply([make_candidate("VRT")], account)
    assert approved == [] and "already holding" in rejected[0]["reason"]


def test_cash_floor_shrinks_then_blocks():
    # equity 100k -> floor 20k. cash 25k leaves 5k of room: position must
    # shrink from 98 shares to 49 (49 * 102 = 4998 <= 5000).
    account = make_account(cash=25_000.0)
    approved, rejected = guardrails.apply(
        [make_candidate("VRT"), make_candidate("ANET")], account
    )
    assert approved[0]["symbol"] == "VRT" and approved[0]["qty"] == 49
    # Second trade finds ~2 dollars of room above the floor -> rejected.
    assert rejected[0]["symbol"] == "ANET" and "cash floor" in rejected[0]["reason"]


def test_malformed_stop_rejected():
    bad = make_candidate(stop=None)
    approved, rejected = guardrails.apply([bad], make_account())
    assert approved == [] and "malformed" in rejected[0]["reason"]

    inverted = make_candidate(stop=110.0)  # stop above close
    approved, rejected = guardrails.apply([inverted], make_account())
    assert approved == [] and "malformed" in rejected[0]["reason"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
