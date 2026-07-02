"""
System B config — Master Brief Part 4 (quality-growth screen).

This folder is deliberately separate from nse-trading-bot/ and has zero
imports from it. It never places orders, never touches Kite, and produces
a ranked research watchlist only — the user makes all buy decisions
manually.

Data source note: the brief specifies FMP + an India-native source
(screener.in/Trendlyne/Tijori). No FMP API key is configured here, so this
uses yfinance instead (free, no key, already proven elsewhere in this
project's codebase) for the QUALITY/GROWTH/VALUATION categories. yfinance
gives ~4-5 years of annual financials for most NSE large/mid-caps, which is
enough for the "consistency over 5 years" checks below, though it's
occasionally thinner than a paid source. If an FMP key is added later,
data_fetch.py is the only file that needs to change.

There is no free automated source for promoter pledging / shareholding
pattern (screener.in has no public API; Trendlyne's is paid) — those two
fields are manual, quarterly-updated entries in manual_redflags.json, not
scraped or estimated.
"""

WATCHLIST = [
    "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS",
    "ASIANPAINT.NS", "TITAN.NS", "PIDILITIND.NS", "NESTLEIND.NS",
    "HINDUNILVR.NS", "BAJFINANCE.NS", "DIVISLAB.NS", "SUNPHARMA.NS",
    "LT.NS", "MARUTI.NS",
]

# --- Scoring weights (must sum to 100, matches the brief's category split) ---
WEIGHT_QUALITY = 35
WEIGHT_GROWTH = 30
WEIGHT_VALUATION = 20
WEIGHT_REDFLAGS = 15

# --- Quality thresholds ---
ROE_EXCELLENT = 0.18   # brief: "consistently above 15-18%"
ROE_GOOD = 0.15
MAX_DEBT_EQUITY = 0.5  # brief: "Debt/Equity below 0.5, or low Debt/EBITDA"

# --- India red flags ---
PLEDGING_HARD_FAIL_PCT = 20.0  # brief default threshold

MANUAL_REDFLAGS_FILE = "manual_redflags.json"
OUTPUT_FILE = "quality_growth_watchlist.json"
