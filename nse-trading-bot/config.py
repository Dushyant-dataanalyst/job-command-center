"""
Locked trading config — Master Brief Part 1. All thresholds trade_filters.py
(and later kite_executor.py / safety_layer.py) use live here, nothing
hardcoded in the filter logic itself, so a threshold change is a one-line
edit instead of a hunt through multiple scripts.

These are NOT the dashboard's circuit breaker thresholds
(DAILY_MAX_LOSS_PCT/WEEKLY_MAX_LOSS_PCT in nse_live_dashboard.html, -3%/-8%)
— that's a separate, already-shipped client-side guard the user asked to
leave untouched. DAILY_LOSS_CAP_PCT/WEEKLY_DRAWDOWN_CAP_PCT below belong to
the Master Brief's not-yet-built Module 3 (safety_layer.py) and intentionally
carry the brief's own numbers (2%/5%). Two different layers, two different
numbers, on purpose.
"""

DRY_RUN = True  # Master Brief: "DRY_RUN=true default. Live orders require explicit false."

# --- Position sizing / risk (Module 1, not yet built) ---
RISK_PCT_MIS = 1.0
RISK_PCT_CNC = 1.5
MAX_POSITION_PCT_OF_CAPITAL = 20.0

# --- Module 3 safety_layer.py (not yet built) — deliberately distinct from
# the dashboard's own -3%/-8% circuit breaker, see module docstring above.
DAILY_LOSS_CAP_PCT = 2.0
WEEKLY_DRAWDOWN_CAP_PCT = 5.0

# --- Module 2 trade_filters.py ---
FILTERS_ENABLED = {
    "market_regime": True,
    "time_window": True,
    "loss_streak": True,
    "risk_reward": True,
}

# Filter 1 — market regime. Reuses market_regime_core.py's own ADX threshold
# (TREND_ADX_THRESHOLD=20) as the single source of truth for "choppy" rather
# than duplicating a second magic number here.
MARKET_REGIME_FILE = "market_regime.json"  # repo-root, written by market_regime_refresh.py

# Filter 2 — time window (IST, "HH:MM" strings)
BLOCK_FIRST_MINUTES_OF_SESSION = 15   # blocks 09:15-09:30
BLOCK_LAST_MINUTES_OF_SESSION = 15    # blocks 15:15-15:30
SESSION_OPEN = "09:15"
SESSION_CLOSE = "15:30"
BLOCK_LUNCH_WINDOW = False            # optional 12:00-13:00 block, off by default
LUNCH_WINDOW_START = "12:00"
LUNCH_WINDOW_END = "13:00"

# Filter 3 — loss streak breaker
LOSS_STREAK_LIMIT = 3  # N consecutive losses (most recent) = no new entries today

# Filter 4 — R:R rejection (checked BEFORE sizing)
MIN_RR_MIS = 1.5
MIN_RR_CNC = 2.0
