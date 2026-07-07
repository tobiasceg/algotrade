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
HISTORY_PERIOD = "6mo"   # how much daily history to download

# News: how far back headlines count as "fresh" for the veto layer
NEWS_HOURS = 24

# Macro events within this many days of today get included in the snapshot
MACRO_HORIZON_DAYS = 7

# Where daily JSON snapshots get written
SNAPSHOT_DIR = "snapshots"
