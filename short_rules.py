"""Step 3b: short-side rules engine. Deterministic, like rules.py.

The mirror image of the long breakout rules, run only when the long book is
structurally blocked (risk-off regime). The two books can never be active on
the same day: longs require QQQ above its 50-day MA, shorts require QQQ
meaningfully below it.

Entry criteria (all must hold on the signal bar):

  1. Regime: benchmark (QQQ) at least SHORT_REGIME_BUFFER_PCT below its
     50-day MA — hysteresis so the first wobble under the MA is not shorted.
  2. Breakdown: close below the prior 20-day low.
  3. Conviction: volume >= 1.5x the 20-day average (same as the long side).
  4. Not extended: close no more than 4% below the 20-day low — down moves
     overshoot and snap back harder than up moves, so tighter than the
     long side's 5%.
  5. Not already crashed: close within 25% of the 20-day high. Shorting a
     name that already fell 30-40% is chasing a stretched rubber band.
  6. Earnings block: no shorts within SHORT_EARNINGS_BLOCK_DAYS of the next
     earnings date, enforced HERE in code (not left to the veto layer,
     which arm A does not have). Unknown earnings date fails closed: a
     missed short is cheap, a gap up through a stop is not.

Survivors become candidate SELL_SHORTs with ATR-sized stops and targets
(stop above, target below), ranked by volume ratio. Empty most days —
same as the long side, that is a feature.

Run standalone against the latest snapshot:  python short_rules.py
Or a specific one:  python short_rules.py snapshots/2026-07-16.json
"""

import json
import sys
from pathlib import Path

import config


def short_regime_on(market: dict) -> bool:
    """Risk-off with hysteresis: benchmark below MA50 by the buffer or more."""
    threshold = market["ma50"] * (1 - config.SHORT_REGIME_BUFFER_PCT / 100)
    return market["close"] < threshold


def generate_candidates(snapshot: dict) -> list[dict]:
    """Apply short-entry rules to a snapshot. Returns candidates ranked by
    volume ratio, or an empty list (a normal, common outcome)."""
    market = snapshot.get("market") or {}
    if not market:
        # No benchmark data — regime unknown. Fail closed: no new trades.
        print("[short] market regime unknown — no candidates")
        return []
    if not short_regime_on(market):
        print(
            f"[short] {market['benchmark']} {market['close']} not at least "
            f"{config.SHORT_REGIME_BUFFER_PCT}% below 50-day MA ({market['ma50']}) "
            "— short book inactive"
        )
        return []

    candidates = []
    for symbol, t in snapshot["tickers"].items():
        required = ("close", "low_20d", "high_20d", "vol_ratio", "atr14")
        if any(t.get(k) is None for k in required):
            print(f"[short] {symbol}: incomplete data, skipped")
            continue

        # Rule 2: breakdown close below the prior 20-day low
        if t["close"] >= t["low_20d"]:
            continue
        # Rule 3: volume surge confirms the breakdown
        if t["vol_ratio"] < config.VOL_SURGE_MIN:
            continue
        # Rule 4: not already extended below the level
        pct_below = t.get("pct_below_low_20d")
        if pct_below is None:
            pct_below = round((1 - t["close"] / t["low_20d"]) * 100, 2)
        if pct_below > config.MAX_BREAKDOWN_EXT_PCT:
            continue
        # Rule 5: the easy part of the move must not have happened already
        crash_pct = round((1 - t["close"] / t["high_20d"]) * 100, 2)
        if crash_pct > config.MAX_CRASH_FROM_HIGH_PCT:
            print(
                f"[short] {symbol}: already down {crash_pct}% from 20d high "
                f"(cap {config.MAX_CRASH_FROM_HIGH_PCT}%) — too late, skipped"
            )
            continue
        # Rule 6: mechanical earnings block, fail-closed on unknown dates
        days_to_earnings = t.get("days_to_earnings")
        if days_to_earnings is None:
            print(f"[short] {symbol}: earnings date unknown — fail closed, skipped")
            continue
        if days_to_earnings <= config.SHORT_EARNINGS_BLOCK_DAYS:
            print(
                f"[short] {symbol}: earnings in {days_to_earnings}d "
                f"(block {config.SHORT_EARNINGS_BLOCK_DAYS}d) — skipped"
            )
            continue

        stop = round(t["close"] + config.STOP_ATR_MULT * t["atr14"], 2)
        target = round(t["close"] - config.TARGET_ATR_MULT * t["atr14"], 2)
        if target <= 0:
            print(f"[short] {symbol}: ATR too large for price, target <= 0 — skipped")
            continue

        candidates.append(
            {
                "symbol": symbol,
                "action": "SELL_SHORT",
                "side": "short",
                "signal_date": t["date"],
                "close": t["close"],
                "stop": stop,
                "target": target,
                "atr14": t["atr14"],
                "vol_ratio": t["vol_ratio"],
                "pct_below_low_20d": pct_below,
                "crash_from_high_pct": crash_pct,
                "risk_per_share": round(stop - t["close"], 2),
                "reward_risk": round(
                    config.TARGET_ATR_MULT / config.STOP_ATR_MULT, 2
                ),
                "reason": (
                    f"closed below 20-day low ({t['low_20d']}) at {t['close']} "
                    f"on {t['vol_ratio']}x average volume; "
                    f"{market['benchmark']} below trend"
                ),
            }
        )

    candidates.sort(key=lambda c: c["vol_ratio"], reverse=True)
    return candidates


def explain(candidates: list[dict]) -> str:
    if not candidates:
        return "[short] no setups today"
    lines = [f"[short] {len(candidates)} candidate(s):"]
    for c in candidates:
        lines.append(
            f"  {c['symbol']}: {c['reason']} -> "
            f"SELL SHORT, stop {c['stop']}, target {c['target']}"
        )
    return "\n".join(lines)


def latest_snapshot_path() -> Path:
    snap_dir = Path(__file__).parent / config.SNAPSHOT_DIR
    snaps = sorted(snap_dir.glob("*.json"))
    if not snaps:
        raise SystemExit("no snapshots found — run data_fetch.py first")
    return snaps[-1]


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_snapshot_path()
    print(f"[short] reading {path}")
    snap = json.loads(path.read_text())
    print(explain(generate_candidates(snap)))
