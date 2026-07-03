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

# Each entry: {"ticker": <yfinance symbol>, "theme": <user-facing thematic
# tag for filtering in the dashboard's Long Term tab>}. "theme" is a manual
# label, not derived from yfinance's own sector/industry classification
# (which is finer-grained and inconsistent for thematic grouping — e.g.
# yfinance calls Olectra "Farm & Heavy Construction Machinery", not "EV").
# All tickers below were verified live against yfinance before being added
# (real longName + current price returned) — a few obvious candidates
# (VATECHWABAG, TATAMOTORS, AMARAJABAT) failed to resolve and were swapped
# for their correct current symbols (WABAG, TMPV, ARE&M respectively).
WATCHLIST = [
    {"ticker": "TCS.NS", "theme": "IT Services"},
    {"ticker": "INFY.NS", "theme": "IT Services"},
    {"ticker": "HDFCBANK.NS", "theme": "Banking"},
    {"ticker": "ICICIBANK.NS", "theme": "Banking"},
    {"ticker": "KOTAKBANK.NS", "theme": "Banking"},
    {"ticker": "ASIANPAINT.NS", "theme": "Chemicals"},
    {"ticker": "TITAN.NS", "theme": "Consumer/Luxury"},
    {"ticker": "PIDILITIND.NS", "theme": "Chemicals"},
    {"ticker": "NESTLEIND.NS", "theme": "FMCG"},
    {"ticker": "HINDUNILVR.NS", "theme": "FMCG"},
    {"ticker": "BAJFINANCE.NS", "theme": "NBFC/Credit"},
    {"ticker": "DIVISLAB.NS", "theme": "Pharma"},
    {"ticker": "SUNPHARMA.NS", "theme": "Pharma"},
    {"ticker": "LT.NS", "theme": "Infra/Capex"},
    {"ticker": "MARUTI.NS", "theme": "Auto"},
    # Water
    {"ticker": "WABAG.NS", "theme": "Water"},
    {"ticker": "IONEXCHANG.NS", "theme": "Water"},
    # Solar
    {"ticker": "WAAREEENER.NS", "theme": "Solar"},
    {"ticker": "PREMIERENE.NS", "theme": "Solar"},
    {"ticker": "WEBELSOLAR.NS", "theme": "Solar"},
    # EV
    {"ticker": "OLECTRA.NS", "theme": "EV"},
    {"ticker": "EXIDEIND.NS", "theme": "EV"},
    {"ticker": "ARE&M.NS", "theme": "EV"},
    {"ticker": "TMPV.NS", "theme": "EV"},
    # Green / Renewable
    {"ticker": "ADANIGREEN.NS", "theme": "Green/Renewable"},
    {"ticker": "NTPCGREEN.NS", "theme": "Green/Renewable"},
    {"ticker": "JSWENERGY.NS", "theme": "Green/Renewable"},
    {"ticker": "SUZLON.NS", "theme": "Green/Renewable"},
    {"ticker": "TATAPOWER.NS", "theme": "Green/Renewable"},
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
