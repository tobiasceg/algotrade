"""Tests for the rules engine, using synthetic snapshots.

Live snapshots rarely contain breakouts, so these hand-built cases are how
each rule gets exercised. Run:  python test_rules.py
"""

import rules


def make_ticker(**overrides) -> dict:
    """A ticker that passes every entry rule; override fields to break one."""
    base = {
        "date": "2026-07-06",
        "close": 142.10,       # above the 20-day high...
        "high_20d": 140.42,    # ...by 1.2% (inside the 5% extension cap)
        "vol_ratio": 1.6,      # above the 1.5x volume floor
        "pct_above_high_20d": 1.2,
        "atr14": 3.15,
        "ma50": 128.00,        # comfortably in its own uptrend
        "trading_days_to_earnings": 20,   # well outside the 2-session block
    }
    base.update(overrides)
    return base


def make_snapshot(tickers: dict, above_trend: bool = True) -> dict:
    return {
        "date": "2026-07-06",
        "market": {
            "benchmark": "QQQ",
            "close": 722.0,
            "ma50": 709.0 if above_trend else 735.0,
            "above_trend": above_trend,
        },
        "tickers": tickers,
    }


def test_clean_breakout_becomes_candidate():
    out = rules.generate_candidates(make_snapshot({"VRT": make_ticker()}))
    assert len(out) == 1, out
    c = out[0]
    assert c["symbol"] == "VRT" and c["action"] == "BUY"
    assert c["stop"] == round(142.10 - 2.0 * 3.15, 2)    # 135.80
    assert c["target"] == round(142.10 + 3.0 * 3.15, 2)  # 151.55
    assert c["stop"] < c["close"] < c["target"]


def test_no_breakout_no_candidate():
    t = make_ticker(close=139.00, pct_above_high_20d=-1.0)
    assert rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_weak_volume_rejected():
    t = make_ticker(vol_ratio=1.2)
    assert rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_overextended_gap_rejected():
    t = make_ticker(close=152.0, pct_above_high_20d=8.2)
    assert rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_risk_off_market_blocks_everything():
    snap = make_snapshot({"VRT": make_ticker()}, above_trend=False)
    assert rules.generate_candidates(snap) == []


def test_missing_data_skipped_not_crashed():
    t = make_ticker(atr14=None)
    assert rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_name_below_its_own_50d_ma_rejected():
    # QQQ can be risk-on while a name is still in its own downtrend — the
    # 2026-08-04 case, where only 13 of 24 names were above their own MA.
    t = make_ticker(ma50=150.0)   # close 142.10 is below it
    assert rules.generate_candidates(make_snapshot({"VRT": t})) == []
    assert rules.blocking_gate(t) == rules.GATE_NAME_TREND


def test_earnings_within_the_block_rejected():
    # ANET on 2026-08-05: a clean breakout reporting the same day, with no
    # veto layer in arm A to catch it.
    t = make_ticker(trading_days_to_earnings=0)
    assert rules.generate_candidates(make_snapshot({"ANET": t})) == []
    assert rules.blocking_gate(t) == rules.GATE_EARNINGS
    assert rules.blocking_gate(make_ticker(trading_days_to_earnings=2)) == rules.GATE_EARNINGS
    assert rules.blocking_gate(make_ticker(trading_days_to_earnings=3)) is None


def test_unknown_earnings_does_not_block_a_long():
    # Unlike a short: the scrape misses ~10-15% of names and a long's
    # downside is bounded, so an unknown date must not silently kill trades.
    t = make_ticker(trading_days_to_earnings=None)
    assert rules.blocking_gate(t) is None
    assert len(rules.generate_candidates(make_snapshot({"VRT": t}))) == 1


def test_breakout_report_names_the_real_gate():
    snap = make_snapshot(
        {
            "ANET": make_ticker(trading_days_to_earnings=0),   # earnings
            "AVGO": make_ticker(vol_ratio=1.46),               # volume
            "NVDA": make_ticker(ma50=150.0),                   # own downtrend
            "MU": make_ticker(close=139.0, pct_above_high_20d=-1.0),  # no breakout
        }
    )
    report = {r["symbol"]: r["gate"] for r in rules.breakout_report(snap)}
    assert "MU" not in report  # never broke out, so not in the report
    assert report["ANET"] == rules.GATE_EARNINGS
    assert report["AVGO"] == rules.GATE_VOLUME
    assert report["NVDA"] == rules.GATE_NAME_TREND


def test_report_and_candidates_never_disagree():
    snap = make_snapshot(
        {"VRT": make_ticker(), "ANET": make_ticker(trading_days_to_earnings=1)}
    )
    passing = {r["symbol"] for r in rules.breakout_report(snap) if r["gate"] is None}
    assert passing == {c["symbol"] for c in rules.generate_candidates(snap)}


def test_ranked_by_volume_conviction():
    snap = make_snapshot(
        {
            "VRT": make_ticker(vol_ratio=1.6),
            "ANET": make_ticker(vol_ratio=2.4),
            "SMCI": make_ticker(vol_ratio=1.9),
        }
    )
    out = rules.generate_candidates(snap)
    assert [c["symbol"] for c in out] == ["ANET", "SMCI", "VRT"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
