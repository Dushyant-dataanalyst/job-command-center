"""
Single market-data choke point. `get_ohlcv()` returns the same shape
`yf_retry.download_with_retry()` does, so it's a drop-in for every signal
script's historical fetch — but it can source candles from the paid Kite
Connect historical API (official, real intraday) instead of unofficial
yfinance.

SAFE BY DEFAULT: the Kite path is gated behind USE_KITE_HISTORICAL (env,
default "false"). With it off, get_ohlcv is byte-for-byte yfinance behavior
— so wiring it into the signal engine changes nothing until we've verified
Kite candles agree with yfinance on a live token (kite_vs_yfinance_check.py)
and deliberately flip the flag on. When on, Kite is tried first only when a
same-day session is live and the symbol resolves to an instrument token;
ANY failure (no session, no token, network, empty candles) falls back to
yfinance. So the worst case is "same as today", never worse.

Returns (df, source) where source is "kite" or "yfinance" so callers can
surface provenance in their output meta.
"""
import sys, os, pathlib
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
import requests

from ist_time import now_ist
from yf_retry import download_with_retry

USE_KITE_HISTORICAL = os.environ.get("USE_KITE_HISTORICAL", "false").lower() == "true"

KITE_HISTORICAL_URL = "https://api.kite.trade/instruments/historical/{token}/{interval}"

# yfinance period string -> lookback days for Kite's from/to window.
_PERIOD_DAYS = {"5d": 7, "1mo": 32, "2mo": 62, "3mo": 95, "6mo": 190, "1y": 370, "3y": 1100}


def _kite_ohlcv(yf_ticker, period, interval):
    """Returns a DataFrame with capitalized OHLCV columns + DatetimeIndex
    (so each caller's existing lowercase-columns transform handles it
    identically to a yfinance frame), or None to signal fall back."""
    from kite_fallback import _load_session
    from kite_instruments import resolve_token

    session = _load_session()
    if session is None:
        return None
    token = resolve_token(yf_ticker)
    if token is None:
        return None

    # An unmapped period used to silently default to 190 days (~6mo) instead
    # of the actually-requested range -- e.g. a "3y" backtest pull would get
    # a truncated-but-real-looking Kite result back and never know it was
    # wrong. Returning None here instead routes it through the SAME
    # already-correct fallback-to-yfinance path every other failure mode in
    # this function uses (no session, no token, bad status, empty candles),
    # so the "never worse than yfinance" contract this module promises still
    # holds -- it's the only fix that doesn't need every failure mode to
    # raise.
    days = _PERIOD_DAYS.get(period)
    if days is None:
        return None
    to_dt = now_ist().date()
    from_dt = to_dt - timedelta(days=days)
    try:
        resp = requests.get(
            KITE_HISTORICAL_URL.format(token=token, interval=interval),
            params={"from": str(from_dt), "to": str(to_dt)},
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {session['api_key']}:{session['access_token']}",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        candles = resp.json().get("data", {}).get("candles", [])
        if not candles:
            return None
        # Kite candle: [timestamp, open, high, low, close, volume]
        df = pd.DataFrame(candles, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
        df.index = pd.to_datetime(df["ts"])
        df = df.drop(columns=["ts"])
        return df if not df.empty else None
    except Exception:
        return None


def get_ohlcv(yf_ticker, period, interval="day"):
    """Drop-in for download_with_retry(ticker, period). Returns (df, source).
    Kite first when enabled+available, else yfinance (always the fallback)."""
    if USE_KITE_HISTORICAL:
        df = _kite_ohlcv(yf_ticker, period, interval)
        if df is not None and not df.empty:
            return df, "kite"
    return download_with_retry(yf_ticker, period=period), "yfinance"
