"""Central configuration: watchlist and data/indicator settings.

Everything the rules engine and data fetch need to agree on lives here,
so tweaking the strategy never means editing pipeline code.
"""

# AI-infrastructure watchlist. Rules engine (step 3) will only ever propose
# trades from this list, and the guardrails (step 5) re-check membership.
WATCHLIST = [
    # GPUs / semis / memory
    "NVDA", "AMD", "AVGO", "TSM", "MU", "MRVL", "ARM",
    # Interconnect, optics, networking
    "ALAB", "CRDO", "COHR", "LITE", "ANET",
    # Servers and system integrators
    "SMCI", "DELL", "HPE", "CLS",
    # Datacenter power and cooling
    "VRT", "ETN", "GEV", "PWR",
    # Power generation for datacenters
    "VST", "CEG", "NRG",
    # AI cloud
    "ORCL",
]

# Market-regime benchmark (trend filter): only take longs when this is
# above its 50-day moving average.
BENCHMARK = "QQQ"

# Indicator lookbacks (trading days)
BREAKOUT_LOOKBACK = 20   # "closed above the 20-day high"
VOLUME_LOOKBACK = 20     # volume vs 20-day average
TREND_MA = 50            # benchmark trend filter
ATR_LOOKBACK = 14        # average true range, for stops/targets
HISTORY_PERIOD = "6mo"   # how much daily history to download

# Entry rules (step 3)
VOL_SURGE_MIN = 1.5          # breakout volume must be >= this x 20-day average
MAX_BREAKOUT_EXT_PCT = 5.0   # skip if close is more than this % above the
                             # 20-day high — a huge gap has already spent the
                             # move, and chasing it wrecks the reward:risk
STOP_ATR_MULT = 2.0          # stop  = close - 2.0 * ATR
TARGET_ATR_MULT = 3.0        # target = close + 3.0 * ATR  (1.5 reward:risk)

# Per-name trend filter: a candidate must be on the right side of its OWN
# 50-day MA, not just the index's. The watchlist spans semiconductors and
# datacenter power, which do not move together — on 2026-08-04 QQQ flipped
# risk-on while only 13 of 24 names were above their own trend, so the index
# gate alone can wave through a name that is still in its own downtrend.
REQUIRE_NAME_TREND = True

# Mechanical earnings block for LONGS, in trading days. Set to match the veto
# layer's stated rule ("earnings within 2 trading days") so arm A and arm B
# block the same trades — if the code and the AI used different windows, the
# arms would differ for a reason that has nothing to do with AI judgment.
# Unknown earnings date does NOT block a long (unlike a short): the scrape
# misses ~10-15% of names, and a long's downside is bounded and size-capped.
LONG_EARNINGS_BLOCK_DAYS = 2

# Hard guardrails (step 5) — enforced by code AFTER any AI approval
MAX_POSITION_PCT = 0.10      # max fraction of equity in one position
MAX_NEW_TRADES_PER_DAY = 2   # new entries per day, across all runs
CASH_FLOOR_PCT = 0.20        # never let cash drop below this fraction of equity
MAX_ENTRY_SLIP_PCT = 2.0     # entry is a DAY limit this % above signal close;
                             # if the stock gaps past it, the order simply
                             # never fills — free protection against chasing

# Exit management (step 6, pre-close run — fully mechanical)
MAX_HOLD_DAYS = 5            # time stop, in trading days; momentum trades
                             # that go nowhere get closed, not babysat

# Close any open position ahead of its earnings report. The entry blocks stop
# us OPENING near earnings, but a 5-day hold can still swallow a report that
# was 3+ sessions away at entry (~8% of trades, given quarterly reporting).
# An earnings gap opens straight through a bracket stop, so this is the only
# protection that works against it.
EXIT_BEFORE_EARNINGS = True
EXIT_BEFORE_EARNINGS_DAYS = 2   # trading days. Two, not one, so that a single
                                # dropped exit run (it has happened — Jul 9)
                                # still leaves tomorrow's pass time to act.

# Short book — mirror of the long rules, active only in a risk-off regime.
# Deliberately NOT a perfect mirror: down moves overshoot and snap back
# harder, gaps go through stops, and squeezes exist, so every asymmetry
# below leans conservative.
SHORT_REGIME_BUFFER_PCT = 1.0   # QQQ must be at least this % BELOW its 50d MA
                                # (hysteresis: no shorting the first wobble)
MAX_BREAKDOWN_EXT_PCT = 4.0     # skip if close is more than this % below the
                                # 20-day low (tighter than the long side's 5)
MAX_CRASH_FROM_HIGH_PCT = 25.0  # don't short a name already down this much
                                # from its 20d high — the easy move happened
SHORT_EARNINGS_BLOCK_DAYS = 5   # mechanical earnings block, in TRADING days
                                # (a gap up through a short's stop is the
                                # worst case in the book; unknown date = skip)
MAX_SHORT_POSITION_PCT = 0.05   # half the long size, same reasoning
SHORT_MAX_HOLD_DAYS = 3         # bear rallies are violent; a short that is
                                # not working quickly is wrong

# Claude veto layer (step 4)
VETO_MODEL = "claude-opus-4-8"
VETO_MAX_TOKENS = 512
EARNINGS_VETO_DAYS = 2       # surfaced to the model as the rule of thumb:
                             # earnings within this many trading days = veto

# News: how far back headlines count as "fresh" for the veto layer
NEWS_HOURS = 24

# Macro events within this many days of today get included in the snapshot
MACRO_HORIZON_DAYS = 7

# Where daily JSON snapshots get written
SNAPSHOT_DIR = "snapshots"
