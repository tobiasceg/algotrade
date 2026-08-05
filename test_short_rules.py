"""Tests for the short-side rules engine, using synthetic snapshots.

Run:  python test_short_rules.py
"""

import short_rules


def make_ticker(**overrides) -> dict:
    """A ticker that passes every short-entry rule; override fields to break one."""
    base = {
        "date": "2026-07-17",
        "close": 96.10,          # below the 20-day low...
        "low_20d": 98.00,        # ...by ~1.9% (inside the 4% extension cap)
        "high_20d": 112.00,      # down ~14% from the high (inside the 25% cap)
        "vol_ratio": 1.7,        # above the 1.5x volume floor
        "pct_below_low_20d": 1.94,
        "atr14": 2.80,
        "ma50": 108.00,          # below its own trend, as a short should be
        "trading_days_to_earnings": 20,  # outside the 5-session earnings block
    }
    base.update(overrides)
    return base


def make_snapshot(tickers: dict, regime: str = "risk_off") -> dict:
    """regime: risk_off (QQQ >1% below MA), wobble (below MA but inside the
    buffer), or risk_on (above MA)."""
    ma50 = 700.0
    close = {"risk_off": 690.0, "wobble": 697.0, "risk_on": 710.0}[regime]
    return {
        "date": "2026-07-17",
        "market": {
            "benchmark": "QQQ",
            "close": close,
            "ma50": ma50,
            "above_trend": close > ma50,
        },
        "tickers": tickers,
    }


def test_clean_breakdown_becomes_candidate():
    out = short_rules.generate_candidates(make_snapshot({"VRT": make_ticker()}))
    assert len(out) == 1, out
    c = out[0]
    assert c["symbol"] == "VRT" and c["action"] == "SELL_SHORT" and c["side"] == "short"
    assert c["stop"] == round(96.10 + 2.0 * 2.80, 2)    # 101.70 — ABOVE the close
    assert c["target"] == round(96.10 - 3.0 * 2.80, 2)  # 87.70 — BELOW the close
    assert c["target"] < c["close"] < c["stop"]


def test_risk_on_market_blocks_everything():
    snap = make_snapshot({"VRT": make_ticker()}, regime="risk_on")
    assert short_rules.generate_candidates(snap) == []


def test_wobble_inside_buffer_blocks_everything():
    # QQQ below its MA but by less than the 1% hysteresis buffer: no shorts.
    snap = make_snapshot({"VRT": make_ticker()}, regime="wobble")
    assert short_rules.generate_candidates(snap) == []


def test_no_breakdown_no_candidate():
    t = make_ticker(close=99.00, pct_below_low_20d=-1.02)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_weak_volume_rejected():
    t = make_ticker(vol_ratio=1.2)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_overextended_breakdown_rejected():
    t = make_ticker(close=92.00, pct_below_low_20d=6.12)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_already_crashed_name_rejected():
    # Down ~31% from the 20-day high: the easy move already happened.
    t = make_ticker(high_20d=140.0)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_earnings_block_enforced_in_code():
    t = make_ticker(trading_days_to_earnings=3)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_unknown_earnings_fails_closed():
    t = make_ticker(trading_days_to_earnings=None)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_missing_data_skipped_not_crashed():
    t = make_ticker(atr14=None)
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_ranked_by_volume_conviction():
    snap = make_snapshot(
        {
            "VRT": make_ticker(vol_ratio=1.6),
            "ANET": make_ticker(vol_ratio=2.4),
            "SMCI": make_ticker(vol_ratio=1.9),
        }
    )
    out = short_rules.generate_candidates(snap)
    assert [c["symbol"] for c in out] == ["ANET", "SMCI", "VRT"]


def test_blocking_gate_names_the_real_reason():
    # The Jul 30 alert claimed "weak volume (best VRT 4.42x)" — but 4.42x
    # clears the 1.5x floor easily; VRT was blocked by the extension cap.
    vrt = make_ticker(close=223.04, low_20d=259.19, high_20d=338.7,
                      vol_ratio=4.44, pct_below_low_20d=13.95)
    assert short_rules.blocking_gate(vrt) == short_rules.GATE_EXTENDED


def test_blocking_gate_covers_each_rule():
    assert short_rules.blocking_gate(make_ticker()) is None
    assert short_rules.blocking_gate(make_ticker(vol_ratio=1.2)) == short_rules.GATE_VOLUME
    assert short_rules.blocking_gate(
        make_ticker(pct_below_low_20d=9.0)
    ) == short_rules.GATE_EXTENDED
    assert short_rules.blocking_gate(
        make_ticker(high_20d=140.0)
    ) == short_rules.GATE_CRASHED
    assert short_rules.blocking_gate(
        make_ticker(trading_days_to_earnings=2)
    ) == short_rules.GATE_EARNINGS
    assert short_rules.blocking_gate(
        make_ticker(trading_days_to_earnings=None)
    ) == short_rules.GATE_EARNINGS_UNKNOWN
    assert short_rules.blocking_gate(
        make_ticker(ma50=90.0)
    ) == short_rules.GATE_NAME_TREND
    assert short_rules.blocking_gate(make_ticker(atr14=None)) == short_rules.GATE_DATA


def test_name_above_its_own_50d_ma_rejected():
    # Breaking a 20-day low while still above its own 50-day MA is a pullback
    # inside an uptrend — the worst kind of short.
    t = make_ticker(ma50=90.0)   # close 96.10 is above it
    assert short_rules.generate_candidates(make_snapshot({"VRT": t})) == []


def test_breakdown_report_lists_gates_not_assumptions():
    snap = make_snapshot(
        {
            "VRT": make_ticker(pct_below_low_20d=13.95, vol_ratio=4.44),  # extended
            "NVDA": make_ticker(vol_ratio=1.13),                          # low volume
            "COHR": make_ticker(high_20d=400.0),                          # crashed
            "ETN": make_ticker(trading_days_to_earnings=2),                       # earnings
            "AMD": make_ticker(close=200.0, low_20d=190.0),               # not broken down
        }
    )
    report = {r["symbol"]: r["gate"] for r in short_rules.breakdown_report(snap)}
    assert "AMD" not in report  # never made a new low, so not in the report
    assert report["VRT"] == short_rules.GATE_EXTENDED
    assert report["NVDA"] == short_rules.GATE_VOLUME
    assert report["COHR"] == short_rules.GATE_CRASHED
    assert report["ETN"] == short_rules.GATE_EARNINGS


def test_report_and_candidates_never_disagree():
    snap = make_snapshot(
        {
            "VRT": make_ticker(),                     # qualifies
            "NVDA": make_ticker(vol_ratio=1.13),      # blocked
        }
    )
    passing = {r["symbol"] for r in short_rules.breakdown_report(snap) if r["gate"] is None}
    assert passing == {c["symbol"] for c in short_rules.generate_candidates(snap)}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")

