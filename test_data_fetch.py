"""Tests for the snapshot freshness gate.

The bot decided on two-day-old prices on 2026-07-15, -27 and -29 because
nothing ever checked that the newest bar was actually the newest session
(Jul 29 missed a valid VRT short as a result). These cover that gate.

Run:  python test_data_fetch.py
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

import data_fetch

ET = ZoneInfo("America/New_York")


def bars_ending(last_day: str) -> pd.DataFrame:
    """A one-row frame whose newest bar is dated last_day."""
    return pd.DataFrame(
        {"Close": [100.0]}, index=pd.DatetimeIndex([pd.Timestamp(last_day)])
    )


def test_latest_session_is_yesterday_during_market_hours():
    # Wed 2026-07-29, 10:00 ET — today's bar is still forming.
    now = datetime.combine(date(2026, 7, 29), time(10, 0), tzinfo=ET)
    assert data_fetch.latest_completed_session(now) == date(2026, 7, 28)


def test_latest_session_is_today_after_the_close_settles():
    now = datetime.combine(date(2026, 7, 29), time(16, 30), tzinfo=ET)
    assert data_fetch.latest_completed_session(now) == date(2026, 7, 29)


def test_latest_session_skips_the_weekend():
    # Monday morning -> the previous Friday.
    now = datetime.combine(date(2026, 7, 27), time(10, 0), tzinfo=ET)
    assert data_fetch.latest_completed_session(now) == date(2026, 7, 24)


def test_latest_session_skips_a_holiday():
    # Fri 2026-07-03 is the observed Independence Day holiday; the Monday
    # after the long weekend must look back to Thursday 2026-07-02.
    now = datetime.combine(date(2026, 7, 6), time(10, 0), tzinfo=ET)
    assert data_fetch.latest_completed_session(now) == date(2026, 7, 2)


def test_stale_tickers_flags_only_the_old_ones():
    bars = {
        "NVDA": bars_ending("2026-07-28"),
        "VRT": bars_ending("2026-07-27"),   # a session behind
        "QQQ": bars_ending("2026-07-28"),
    }
    assert data_fetch.stale_tickers(bars, date(2026, 7, 28)) == ["VRT"]


def test_the_jul_29_failure_is_now_detected():
    # Every ticker a session behind — exactly what happened on Jul 29,
    # when the run silently analysed Jul 27 believing it was Jul 28.
    bars = {s: bars_ending("2026-07-27") for s in ("NVDA", "VRT", "QQQ")}
    stale = data_fetch.stale_tickers(bars, date(2026, 7, 28))
    assert stale == ["NVDA", "QQQ", "VRT"]
    assert "QQQ" in stale  # benchmark stale -> run must abort


def test_trading_days_to_earnings_crosses_the_weekend():
    # Thu 2026-08-06 looking at Mon 2026-08-10 earnings: 4 calendar days but
    # only 2 sessions away. Counting calendar days would miss the block.
    sessions = data_fetch.upcoming_sessions(date(2026, 8, 6))
    assert (date(2026, 8, 10) - date(2026, 8, 6)).days == 4
    assert data_fetch.trading_days_until(date(2026, 8, 10), sessions) == 2


def test_trading_days_to_earnings_same_day_is_zero():
    sessions = data_fetch.upcoming_sessions(date(2026, 8, 5))
    assert data_fetch.trading_days_until(date(2026, 8, 5), sessions) == 0
    assert data_fetch.trading_days_until(date(2026, 8, 6), sessions) == 1


def test_trading_days_beyond_the_horizon_is_unknown():
    sessions = data_fetch.upcoming_sessions(date(2026, 8, 5), horizon_days=10)
    assert data_fetch.trading_days_until(date(2027, 1, 4), sessions) is None


def test_fresh_bars_report_nothing_stale():
    bars = {s: bars_ending("2026-07-28") for s in ("NVDA", "VRT", "QQQ")}
    assert data_fetch.stale_tickers(bars, date(2026, 7, 28)) == []


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_report_after_the_signal_bar_makes_it_stale():
    # The live 2026-08-05 case: ANET reported at 16:00 ET on Aug 4, the very
    # bar the breakout was measured on, then gapped 10% at Wednesday's open.
    signal_close = data_fetch.session_close(date(2026, 8, 4))
    assert data_fetch.signal_overtaken_by_earnings(
        et(2026, 8, 4, 16, 0), signal_close, et(2026, 8, 5, 10, 0)
    )


def test_report_before_the_signal_bar_closed_is_fine():
    # Reported pre-open on the signal day: that close already reflects the
    # news, so the breakout is a legitimate post-earnings setup.
    signal_close = data_fetch.session_close(date(2026, 8, 4))
    assert not data_fetch.signal_overtaken_by_earnings(
        et(2026, 8, 4, 8, 0), signal_close, et(2026, 8, 5, 10, 0)
    )


def test_future_report_is_not_stale():
    signal_close = data_fetch.session_close(date(2026, 8, 4))
    assert not data_fetch.signal_overtaken_by_earnings(
        et(2026, 8, 27, 16, 0), signal_close, et(2026, 8, 5, 10, 0)
    )


def test_unknown_timestamp_keeps_the_candidate():
    signal_close = data_fetch.session_close(date(2026, 8, 4))
    assert not data_fetch.signal_overtaken_by_earnings(
        None, signal_close, et(2026, 8, 5, 10, 0)
    )


def test_drop_stale_signals_splits_the_list():
    cands = [
        {"symbol": "ANET", "signal_date": "2026-08-04"},   # reported after
        {"symbol": "NVDA", "signal_date": "2026-08-04"},   # reports later
        {"symbol": "AVGO", "signal_date": "2026-08-04"},   # unknown
    ]
    stamps = {
        "ANET": et(2026, 8, 4, 16, 0),
        "NVDA": et(2026, 8, 26, 16, 0),
        "AVGO": None,
    }
    kept, dropped = data_fetch.drop_stale_signals(
        cands, et(2026, 8, 5, 10, 0), fetch=stamps.get
    )
    assert [c["symbol"] for c in kept] == ["NVDA", "AVGO"]
    assert [d["symbol"] for d in dropped] == ["ANET"]
    assert "stale" in dropped[0]["reason"] and "2026-08-04" in dropped[0]["reason"]


def test_half_day_close_is_respected():
    # 2026-11-27 is the day after Thanksgiving: a 13:00 ET close. A 14:00
    # report is therefore AFTER that bar, though before a normal 16:00 close.
    half = data_fetch.session_close(date(2026, 11, 27))
    assert half.hour == 13, half
    assert data_fetch.signal_overtaken_by_earnings(
        et(2026, 11, 27, 14, 0), half, et(2026, 11, 30, 10, 0)
    )


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(tests)} tests passed")
