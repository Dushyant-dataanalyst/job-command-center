"""
yfinance data fetch for System B — standalone, no dependency on
nse-trading-bot/ (this folder must stay independently importable/runnable).
"""
import time

import yfinance as yf


def fetch_stock_data(ticker, retries=3, backoff_seconds=5):
    """Returns {"info": dict, "financials": DataFrame, "balance_sheet": DataFrame,
    "cashflow": DataFrame} or {"error": str} after retries are exhausted."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            tk = yf.Ticker(ticker)
            info = tk.info
            if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                raise ValueError("empty/incomplete info response")
            return {
                "info": info,
                "financials": tk.financials,
                "balance_sheet": tk.balance_sheet,
                "cashflow": tk.cashflow,
            }
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff_seconds * (2 ** attempt))
    return {"error": str(last_exc)}
