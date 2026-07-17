"""Step 2: data fetch. Plain Python, no AI.

Pulls daily bars for the watchlist + benchmark, computes the indicators the
rules engine needs, loads the macro event calendar, and grabs recent
headlines and the next earnings date per ticker. Everything is assembled
into one structured JSON snapshot — the single source of truth that every
later stage (rules, veto, logging) reads from.

Data source is yfinance (Yahoo Finance): no API key, consolidated volume
(important — the volume-surge signal needs real market-wide volume, which
free Alpaca/IEX feeds understate). Alpaca is only used later, for execution.

Run standalone:  python data_fetch.py
"""

import json
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

import config

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------- bars

def fetch_bars(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download daily OHLCV history for all tickers in one batch request.

    Returns {ticker: DataFrame} containing only COMPLETED sessions: when this
    runs during market hours (the 10:00 ET entry run), Yahoo includes a
    partial bar for today, which would poison "closed above the 20-day high"
    with a half-formed close. We drop it and let the rules work off
    yesterday's completed bar.
    """
    raw = yf.download(
        tickers,
        period=config.HISTORY_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    now_et = datetime.now(tz=ET)
    # Today's bar counts as complete only well after the 16:00 close
    # (consolidated prints settle a few minutes late).
    today_is_complete = now_et.time() >= time(16, 20)

    bars = {}
    for ticker in tickers:
        try:
            df = raw[ticker].dropna(how="all")
        except KeyError:
            print(f"[bars] WARNING: no data returned for {ticker}")
            continue
        if df.empty:
            print(f"[bars] WARNING: empty history for {ticker}")
            continue
        if df.index[-1].date() >= now_et.date() and not today_is_complete:
            df = df.iloc[:-1]
        bars[ticker] = df
    return bars


def compute_indicators(df: pd.DataFrame) -> dict:
    """Indicators for one ticker, from completed daily bars.

    The signal bar is the most recent completed session. Breakout and volume
    baselines are computed over the 20 days BEFORE the signal bar, so the
    signal bar can't inflate its own reference level.
    """
    signal = df.iloc[-1]
    prior = df.iloc[:-1]

    high_20d = prior["High"].tail(config.BREAKOUT_LOOKBACK).max()
    low_20d = prior["Low"].tail(config.BREAKOUT_LOOKBACK).min()
    avg_vol_20d = prior["Volume"].tail(config.VOLUME_LOOKBACK).mean()

    # True range = max(high-low, |high-prev close|, |low-prev close|);
    # ATR is its simple average. Used to size stops to each name's volatility.
    prev_close = df["Close"].shift(1)
    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.tail(config.ATR_LOOKBACK).mean()

    return {
        "date": df.index[-1].date().isoformat(),
        "close": round(float(signal["Close"]), 2),
        "high": round(float(signal["High"]), 2),
        "low": round(float(signal["Low"]), 2),
        "volume": int(signal["Volume"]),
        "high_20d": round(float(high_20d), 2),
        "low_20d": round(float(low_20d), 2),
        "avg_vol_20d": int(avg_vol_20d),
        "vol_ratio": round(float(signal["Volume"] / avg_vol_20d), 2),
        "ma20": round(float(df["Close"].tail(20).mean()), 2),
        "ma50": round(float(df["Close"].tail(50).mean()), 2),
        "atr14": round(float(atr), 2),
        "pct_above_high_20d": round(
            float((signal["Close"] / high_20d - 1) * 100), 2
        ),
        # Positive when the close is BELOW the 20-day low (short-side mirror)
        "pct_below_low_20d": round(
            float((1 - signal["Close"] / low_20d) * 100), 2
        ),
    }


# ------------------------------------------------------- calendars & news

def load_macro_events(today: date) -> list[dict]:
    """Upcoming scheduled macro events (FOMC, CPI) within the horizon."""
    path = Path(__file__).parent / "macro_calendar.json"
    events = json.loads(path.read_text())["events"]
    horizon = today + timedelta(days=config.MACRO_HORIZON_DAYS)
    upcoming = [
        {**e, "days_away": (date.fromisoformat(e["date"]) - today).days}
        for e in events
        if today <= date.fromisoformat(e["date"]) <= horizon
    ]
    if not upcoming:
        return []
    return upcoming


def fetch_earnings_date(ticker: yf.Ticker, today: date) -> dict:
    """Next scheduled earnings date for one ticker, or None if unknown.

    yfinance earnings data is scraped and occasionally missing — treat an
    unknown date as a fact worth surfacing (the veto prompt can see
    "earnings_date": null and weigh that), never as a crash.
    """
    try:
        cal = ticker.calendar or {}
        dates = cal.get("Earnings Date") or []
        future = sorted(d for d in dates if d >= today)
        if not future:
            return {"earnings_date": None, "days_to_earnings": None}
        nxt = future[0]
        return {
            "earnings_date": nxt.isoformat(),
            "days_to_earnings": (nxt - today).days,
        }
    except Exception as exc:  # noqa: BLE001 — third-party scrape, any failure is non-fatal
        print(f"[earnings] WARNING {ticker.ticker}: {exc}")
        return {"earnings_date": None, "days_to_earnings": None}


def fetch_news(ticker: yf.Ticker, now_utc: datetime) -> list[dict]:
    """Headlines from the last NEWS_HOURS for one ticker.

    Handles both yfinance news schemas (newer nests everything under
    'content'; older is flat with a unix timestamp).
    """
    cutoff = now_utc - timedelta(hours=config.NEWS_HOURS)
    headlines = []
    try:
        items = ticker.news or []
    except Exception as exc:  # noqa: BLE001
        print(f"[news] WARNING {ticker.ticker}: {exc}")
        return []

    for item in items:
        content = item.get("content", item)
        title = content.get("title")
        if not title:
            continue

        published = None
        raw_time = content.get("pubDate") or item.get("providerPublishTime")
        if isinstance(raw_time, str):
            try:
                published = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except ValueError:
                pass
        elif isinstance(raw_time, (int, float)):
            published = datetime.fromtimestamp(raw_time, tz=timezone.utc)

        if published is None or published < cutoff:
            continue

        provider = content.get("provider") or {}
        headlines.append(
            {
                "time": published.astimezone(timezone.utc).isoformat(),
                "title": title,
                "publisher": provider.get("displayName")
                or item.get("publisher")
                or "unknown",
            }
        )
    return headlines


# ------------------------------------------------------------- snapshot

def build_snapshot() -> dict:
    """Assemble the full daily snapshot: market regime, macro calendar,
    and per-ticker indicators + earnings + headlines."""
    now_et = datetime.now(tz=ET)
    now_utc = datetime.now(tz=timezone.utc)
    today = now_et.date()

    all_tickers = config.WATCHLIST + [config.BENCHMARK]
    print(f"[snapshot] fetching daily bars for {len(all_tickers)} tickers...")
    bars = fetch_bars(all_tickers)

    # Market regime from the benchmark
    market = {}
    if config.BENCHMARK in bars:
        bench = compute_indicators(bars[config.BENCHMARK])
        market = {
            "benchmark": config.BENCHMARK,
            "close": bench["close"],
            "ma50": bench["ma50"],
            "above_trend": bench["close"] > bench["ma50"],
        }
    else:
        print("[snapshot] WARNING: no benchmark data — market regime unknown")

    macro_events = load_macro_events(today)

    tickers_out = {}
    for symbol in config.WATCHLIST:
        if symbol not in bars:
            continue
        entry = compute_indicators(bars[symbol])
        yft = yf.Ticker(symbol)
        entry.update(fetch_earnings_date(yft, today))
        entry["news"] = fetch_news(yft, now_utc)
        tickers_out[symbol] = entry
        time_mod.sleep(0.3)  # be polite to Yahoo; ~25 tickers stays under a minute

    return {
        "date": today.isoformat(),
        "generated_at": now_et.isoformat(),
        "market": market,
        "macro_events": macro_events,
        "tickers": tickers_out,
    }


def save_snapshot(snapshot: dict) -> Path:
    out_dir = Path(__file__).parent / config.SNAPSHOT_DIR
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{snapshot['date']}.json"
    path.write_text(json.dumps(snapshot, indent=2))
    return path


def summarize(snapshot: dict) -> str:
    """One-glance text summary, reused later for logs and Telegram."""
    lines = [f"snapshot {snapshot['date']}: {len(snapshot['tickers'])} tickers"]
    m = snapshot["market"]
    if m:
        trend = "ABOVE" if m["above_trend"] else "BELOW"
        lines.append(
            f"  {m['benchmark']} {m['close']} — {trend} 50-day MA ({m['ma50']})"
        )
    for e in snapshot["macro_events"]:
        lines.append(f"  macro: {e['event']} on {e['date']} ({e['days_away']}d away)")
    total_news = sum(len(t["news"]) for t in snapshot["tickers"].values())
    lines.append(f"  headlines in last {config.NEWS_HOURS}h: {total_news}")
    near_earnings = [
        f"{s} ({t['days_to_earnings']}d)"
        for s, t in snapshot["tickers"].items()
        if t["days_to_earnings"] is not None and t["days_to_earnings"] <= 7
    ]
    if near_earnings:
        lines.append(f"  earnings within 7d: {', '.join(near_earnings)}")
    return "\n".join(lines)


if __name__ == "__main__":
    snap = build_snapshot()
    path = save_snapshot(snap)
    print(summarize(snap))
    print(f"[snapshot] written to {path}")
