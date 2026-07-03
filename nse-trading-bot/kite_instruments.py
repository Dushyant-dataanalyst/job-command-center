"""
Kite instrument-token mapping. Kite's historical-data API is keyed by a
numeric instrument_token, not the trading symbol, so we need symbol->token
resolution. The full NSE instrument list is a CSV dump from
/instruments/NSE (~2000+ rows); we fetch it at most once per trading day and
cache symbol->token to _kite_instruments_cache.json so the 5-min cron
doesn't re-download it every run.

Indices (NIFTY 50, NIFTY BANK, INDIA VIX) aren't in the equity dump, so
their tokens are hardcoded constants below — well-known and stable, but
verify against Kite if a lookup ever returns unexpected candles.

Everything here fails soft: any failure (no session, network, parse) returns
None so the caller falls back to yfinance rather than crashing.
"""
import sys, os, json, csv, io, pathlib

sys.path.insert(0, os.path.dirname(__file__))

import requests

from ist_time import now_ist
from kite_fallback import _load_session

CACHE_FILE = pathlib.Path(__file__).parent / "_kite_instruments_cache.json"
KITE_INSTRUMENTS_NSE_URL = "https://api.kite.trade/instruments/NSE"

# Well-known, stable index tokens (not present in the equity dump).
INDEX_TOKENS = {
    "^NSEI": 256265,      # NIFTY 50
    "^NSEBANK": 260105,   # NIFTY BANK
    "^INDIAVIX": 264969,  # INDIA VIX
}


def _fetch_instruments():
    session = _load_session()
    if session is None:
        return None
    try:
        resp = requests.get(
            KITE_INSTRUMENTS_NSE_URL,
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {session['api_key']}:{session['access_token']}",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        mapping = {}
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            # EQ only — skip F&O/other instrument types that share a symbol.
            if row.get("instrument_type") == "EQ" and row.get("tradingsymbol"):
                try:
                    mapping[row["tradingsymbol"]] = int(row["instrument_token"])
                except (ValueError, KeyError):
                    continue
        return mapping or None
    except Exception:
        return None


def _load_cache():
    if not CACHE_FILE.exists():
        return None
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _get_equity_map():
    """Returns {tradingsymbol: instrument_token}, refetching the dump only
    once per trading day. Returns {} if unavailable (caller falls back)."""
    today = now_ist().strftime("%Y-%m-%d")
    cache = _load_cache()
    if cache and cache.get("cached_date") == today and isinstance(cache.get("map"), dict):
        return cache["map"]

    fetched = _fetch_instruments()
    if fetched is None:
        # Stale cache is better than nothing for token lookup (tokens don't
        # change day to day), so fall back to it if the refetch failed.
        return cache["map"] if cache and isinstance(cache.get("map"), dict) else {}

    CACHE_FILE.write_text(json.dumps({"cached_date": today, "map": fetched}, indent=2), encoding="utf-8")
    return fetched


def resolve_token(yf_ticker):
    """Map a yfinance-style ticker to a Kite instrument_token, or None.
      'RELIANCE.NS' -> NSE equity token
      '^NSEI' / '^NSEBANK' / '^INDIAVIX' -> index constants
    None means 'no Kite token' — caller should use yfinance."""
    if yf_ticker in INDEX_TOKENS:
        return INDEX_TOKENS[yf_ticker]
    if yf_ticker.endswith(".NS"):
        symbol = yf_ticker[:-3]
        return _get_equity_map().get(symbol)
    return None
