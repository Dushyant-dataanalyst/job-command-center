"""
Kite Connect client — two roles:

A. QUOTE FALLBACK (used only when yfinance fails):
   1. Tracked equity/F&O position P&L (equity_brain.py) — a clean, complete
      fallback. P&L only needs current price vs. entry price, and Kite's
      quote/ltp endpoint gives exactly that.
   2. NIFTY/BANKNIFTY F&O signal (refresh_fo_cloud.py) — a PARTIAL fallback
      only. Kite's simple quote API gives current price, not 6 months of
      historical OHLCV, so it cannot reconstruct the EMA/RSI/MACD/ADX signal
      yfinance provides. When yfinance is down, this returns the live spot
      price so the dashboard isn't blank, but the caller must mark the
      consensus signal as unavailable rather than fabricating indicators
      from a single price point.

B. REAL PORTFOLIO READS (kite_portfolio_refresh.py) — this is the paid
   ₹2000/month Kite Connect being actually used, not just as a fallback:
   get_holdings/get_positions/get_margins/get_trades/get_orders pull the
   user's real Zerodha account data (demat holdings, open positions,
   available margin, today's fills/orders). Read-only — no order placement
   (that needs a SEBI static whitelisted IP, impossible on GH Actions).

Everything here requires a same-day kite_session.json (see
kite_auth_refresh.py) — Kite access_tokens expire daily, so every call
fails closed (returns None / (None, reason)) if the session is missing or
from a previous trading day, rather than trying a stale/invalid token.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import requests

from ist_time import now_ist

REPO_ROOT = pathlib.Path(__file__).parent.parent
SESSION_FILE = REPO_ROOT / "kite_session.json"

KITE_QUOTE_LTP_URL = "https://api.kite.trade/quote/ltp"
KITE_TRADES_URL = "https://api.kite.trade/trades"
KITE_ORDERS_URL = "https://api.kite.trade/orders"
KITE_HOLDINGS_URL = "https://api.kite.trade/portfolio/holdings"
KITE_POSITIONS_URL = "https://api.kite.trade/portfolio/positions"
KITE_MARGINS_URL = "https://api.kite.trade/user/margins"

# Verify these against your own Kite account before relying on them —
# exact index symbol strings can vary and aren't independently testable
# here without a live Kite session.
INDEX_SYMBOLS = {
    "NIFTY50": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
}


def _load_session():
    if not SESSION_FILE.exists():
        return None
    try:
        session = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        today = now_ist().strftime("%Y-%m-%d")
        if session.get("trading_date") != today:
            return None  # yesterday's token — Kite invalidates it, don't even try
        return session
    except Exception:
        return None


def get_ltp(symbols):
    """symbols: list of 'EXCHANGE:TRADINGSYMBOL' strings (e.g. 'NSE:RELIANCE').
    Returns {symbol: last_price} for whichever symbols Kite returned, or None
    if the session is unavailable/invalid or the request itself failed."""
    session = _load_session()
    if session is None:
        return None
    try:
        resp = requests.get(
            KITE_QUOTE_LTP_URL,
            params=[("i", s) for s in symbols],
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {session['api_key']}:{session['access_token']}",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        return {k: v.get("last_price") for k, v in data.items() if "last_price" in v}
    except Exception:
        return None


def get_index_spot(index_name):
    """index_name: 'NIFTY50' or 'BANKNIFTY'. Returns float spot price or None."""
    symbol = INDEX_SYMBOLS.get(index_name)
    if not symbol:
        return None
    result = get_ltp([symbol])
    if not result:
        return None
    return result.get(symbol)


def get_stock_spot(nse_symbol):
    """nse_symbol: bare NSE symbol, e.g. 'RELIANCE' (no .NS suffix, no exchange prefix)."""
    symbol = f"NSE:{nse_symbol}"
    result = get_ltp([symbol])
    if not result:
        return None
    return result.get(symbol)


def _authed_get(url):
    session = _load_session()
    if session is None:
        return None, "no valid Kite session for today — run kite_auth_refresh.py (see kite_auth_refresh.yml for the login URL)"
    try:
        resp = requests.get(
            url,
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {session['api_key']}:{session['access_token']}",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None, f"Kite API returned {resp.status_code}: {resp.text}"
        return resp.json().get("data", []), None
    except Exception as e:
        return None, f"request failed: {e}"


def get_trades():
    """Today's executed trades (fills) on your Kite account — real broker
    data, not the paper-trading journal. Returns (trades, error) where
    trades is a list of Kite's raw trade dicts (trading_symbol, transaction_type,
    quantity, average_price, trade_id, order_id, fill_timestamp, ...) or
    (None, reason) if no valid session / the request failed."""
    return _authed_get(KITE_TRADES_URL)


def get_orders():
    """Today's order book (all statuses: COMPLETE/OPEN/CANCELLED/REJECTED),
    same (data, error) shape as get_trades()."""
    return _authed_get(KITE_ORDERS_URL)


def get_holdings():
    """Long-term (delivery/CNC) demat holdings — your real Zerodha portfolio.
    Returns (holdings, error) where holdings is a list of Kite holding dicts
    (tradingsymbol, exchange, quantity, average_price, last_price, close_price,
    pnl, day_change, day_change_percentage, ...) or (None, reason)."""
    return _authed_get(KITE_HOLDINGS_URL)


def get_positions():
    """Intraday/F&O positions. Kite returns a dict {"net": [...], "day": [...]}
    (not a flat list), so callers should read .get('net')/.get('day').
    Returns (positions_dict, error) or (None, reason)."""
    return _authed_get(KITE_POSITIONS_URL)


def get_margins():
    """Account margins/available funds. Kite returns a dict with 'equity' and
    'commodity' segments, each with an 'available' block (live_balance,
    cash, etc.). Returns (margins_dict, error) or (None, reason)."""
    return _authed_get(KITE_MARGINS_URL)
