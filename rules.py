"""Step 3: rules engine. Deterministic — same snapshot in, same trades out.

Reads the daily JSON snapshot and applies the entry criteria:

  1. Market regime: benchmark (QQQ) must be above its 50-day MA.
  2. Breakout: yesterday's close above the prior 20-day high.
  3. Conviction: breakout volume >= 1.5x the 20-day average.
  4. Not extended: close no more than 5% above the 20-day high
     (a huge gap has already spent the move — chasing it ruins reward:risk).
  5. Own trend: the name itself above its 50-day MA. The index gate cannot
     speak for both semiconductors and datacenter power.
  6. Earnings block: no entry within LONG_EARNINGS_BLOCK_DAYS trading days
     of a scheduled report. A gap through the stop is the one thing a
     bracket cannot protect against, and this must hold in arm A too,
     where there is no veto layer to catch it.

Survivors become candidate BUYs with ATR-sized stops and targets, ranked by
volume ratio (strongest conviction first). Many days the list is empty —
that is a feature, not a bug: the AI veto layer is only ever shown trades
this code already wanted, never asked to go find one.

Run standalone against the latest snapshot:  python rules.py
Or a specific one:  python rules.py snapshots/2026-07-06.json
"""

import json
import sys
from pathlib import Path

import config


# Gate labels, in the order applied. Tallied into the Telegram alert, so a
# night where names broke out but nothing traded explains itself accurately.
GATE_VOLUME = "low volume"
GATE_EXTENDED = "too extended"
GATE_NAME_TREND = "below own 50d MA"
GATE_EARNINGS = "near earnings"
GATE_DATA = "incomplete data"


def blocking_gate(t: dict) -> str | None:
    """Which gate rejects this already-broken-out ticker, or None if it
    passes every one. Single source of truth: generate_candidates and the
    alert diagnostics both read the verdict from here, so the reported
    reason can never drift from the real one."""
    required = ("close", "high_20d", "vol_ratio", "atr14")
    if any(t.get(k) is None for k in required):
        return GATE_DATA

    if t["vol_ratio"] < config.VOL_SURGE_MIN:
        return GATE_VOLUME

    if t["pct_above_high_20d"] > config.MAX_BREAKOUT_EXT_PCT:
        return GATE_EXTENDED

    if config.REQUIRE_NAME_TREND:
        ma50 = t.get("ma50")
        if ma50 is None or t["close"] <= ma50:
            return GATE_NAME_TREND

    # Unknown earnings date deliberately does NOT block a long — see config.
    ted = t.get("trading_days_to_earnings")
    if ted is not None and ted <= config.LONG_EARNINGS_BLOCK_DAYS:
        return GATE_EARNINGS

    return None


def breakout_report(snapshot: dict) -> list[dict]:
    """Every name that closed above its prior 20-day high, with the gate that
    actually blocked it (None if it qualified). Diagnostic for the alert."""
    report = []
    for symbol, t in snapshot.get("tickers", {}).items():
        close, high_20d = t.get("close"), t.get("high_20d")
        if close is None or high_20d is None or close <= high_20d:
            continue
        report.append({"symbol": symbol, "gate": blocking_gate(t)})
    return report


def generate_candidates(snapshot: dict) -> list[dict]:
    """Apply entry rules to a snapshot. Returns candidates ranked by
    volume ratio, or an empty list (a normal, common outcome)."""
    market = snapshot.get("market") or {}
    if not market:
        # No benchmark data — regime unknown. Fail closed: no new trades.
        print("[rules] market regime unknown — no candidates")
        return []
    if not market["above_trend"]:
        print(
            f"[rules] {market['benchmark']} {market['close']} below 50-day MA "
            f"({market['ma50']}) — risk-off, no new entries"
        )
        return []

    candidates = []
    for symbol, t in snapshot["tickers"].items():
        # Rule 2: breakout close above the prior 20-day high. Anything that
        # is not even a breakout is silently uninteresting.
        if t.get("close") is None or t.get("high_20d") is None:
            print(f"[rules] {symbol}: incomplete data, skipped")
            continue
        if t["close"] <= t["high_20d"]:
            continue

        # Rules 3-6 live in blocking_gate so the alert cannot misreport them.
        gate = blocking_gate(t)
        if gate is not None:
            print(f"[rules] {symbol}: {gate}, skipped")
            continue

        stop = round(t["close"] - config.STOP_ATR_MULT * t["atr14"], 2)
        target = round(t["close"] + config.TARGET_ATR_MULT * t["atr14"], 2)

        candidates.append(
            {
                "symbol": symbol,
                "action": "BUY",
                "side": "long",
                "signal_date": t["date"],
                "close": t["close"],
                "stop": stop,
                "target": target,
                "atr14": t["atr14"],
                "vol_ratio": t["vol_ratio"],
                "pct_above_high_20d": t["pct_above_high_20d"],
                "risk_per_share": round(t["close"] - stop, 2),
                "reward_risk": round(
                    config.TARGET_ATR_MULT / config.STOP_ATR_MULT, 2
                ),
                "reason": (
                    f"closed above 20-day high ({t['high_20d']}) at {t['close']} "
                    f"on {t['vol_ratio']}x average volume; "
                    f"{market['benchmark']} above trend"
                ),
            }
        )

    # Strongest volume conviction first — if guardrails later cap the number
    # of trades per day, the best setups are taken first.
    candidates.sort(key=lambda c: c["vol_ratio"], reverse=True)
    return candidates


def explain(candidates: list[dict]) -> str:
    if not candidates:
        return "[rules] no setups today"
    lines = [f"[rules] {len(candidates)} candidate(s):"]
    for c in candidates:
        lines.append(
            f"  {c['symbol']}: {c['reason']} -> "
            f"BUY, stop {c['stop']}, target {c['target']}"
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
    print(f"[rules] reading {path}")
    snap = json.loads(path.read_text())
    print(explain(generate_candidates(snap)))
