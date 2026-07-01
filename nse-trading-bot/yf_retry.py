"""
Shared retry wrapper for yfinance calls. A transient network blip
(timeout, connection reset) shouldn't fail an entire CI run when a
single retry would have succeeded.
"""
import time

import yfinance as yf


def download_with_retry(ticker, period, retries=3, backoff_seconds=5, **kwargs):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            df = yf.download(ticker, period=period, progress=False, auto_adjust=True, **kwargs)
            if not df.empty:
                return df
            last_exc = None  # empty result, not an exception — don't retry forever on a genuinely empty ticker
            if attempt < retries:
                time.sleep(backoff_seconds * (2 ** attempt))  # 5s, 10s, 20s
                continue
            return df
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff_seconds * (2 ** attempt))  # 5s, 10s, 20s
    if last_exc:
        raise last_exc
    import pandas as pd
    return pd.DataFrame()
