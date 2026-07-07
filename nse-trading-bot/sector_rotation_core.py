"""
Core sector-rotation scan logic, shared by:
  - mcp_servers/sector_rotation/server.py (live MCP tool for Claude)
  - sector_rotation_refresh.py (CI cron that feeds the dashboard JSON)

No MCP SDK dependency here — just yfinance/pandas. Every number is fetched
live at call time; nothing is cached or pre-baked. See module docstring in
the MCP server for the full rationale on scoring and data honesty.
"""
import pandas as pd

from market_data import get_ohlcv
from ist_time import now_ist_str

SECTOR_INDICES = {
    "Banking":  "^NSEBANK",
    "IT":       "^CNXIT",
    "Auto":     "^CNXAUTO",
    "Pharma":   "^CNXPHARMA",
    "FMCG":     "^CNXFMCG",
    "Metal":    "^CNXMETAL",
    "Energy":   "^CNXENERGY",
    "Realty":   "^CNXREALTY",
    "PSU Bank": "^CNXPSUBANK",
    "Media":    "^CNXMEDIA",
}

# Static sector -> representative large-cap constituents (membership
# classification, not a market-data feed — does not go stale day to day).
SECTOR_STOCKS = {
    "Banking":  ["HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "KOTAKBANK.NS", "AXISBANK.NS"],
    "IT":       ["TCS.NS", "INFY.NS", "WIPRO.NS", "HCLTECH.NS", "TECHM.NS"],
    "Auto":     ["MARUTI.NS", "TMPV.NS", "M&M.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS"],
    "Pharma":   ["SUNPHARMA.NS", "DRREDDY.NS", "CIPLA.NS", "DIVISLAB.NS", "AUROPHARMA.NS"],
    "FMCG":     ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "DABUR.NS"],
    "Metal":    ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS", "SAIL.NS"],
    "Energy":   ["RELIANCE.NS", "ONGC.NS", "POWERGRID.NS", "NTPC.NS", "BPCL.NS"],
    "Realty":   ["DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PHOENIXLTD.NS", "PRESTIGE.NS"],
    "PSU Bank": ["SBIN.NS", "BANKBARODA.NS", "PNB.NS", "CANBK.NS", "UNIONBANK.NS"],
    "Media":    ["ZEEL.NS", "SUNTV.NS", "PVRINOX.NS", "NETWORK18.NS", "SAREGAMA.NS"],
}


def _momentum_volume(ticker, period="2mo"):
    df, data_source = get_ohlcv(ticker, period=period)
    if df.empty or len(df) < 25:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    close, vol = df["close"], df["volume"]
    roc5 = float(close.pct_change(5).iloc[-1] * 100)
    # Today's bar can still be mid-formation (volume=0 if yfinance hasn't
    # rolled it up yet) — use the most recent bar that actually has volume
    # so relative volume isn't falsely zeroed out intraday.
    vol_nonzero = vol[vol > 0]
    if len(vol_nonzero) < 21:
        return None
    last_vol = float(vol_nonzero.iloc[-1])
    avg_vol20 = float(vol_nonzero.iloc[-21:-1].mean())
    rel_vol = round(last_vol / avg_vol20, 2) if avg_vol20 else None
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    bullish = bool(close.iloc[-1] > ema9.iloc[-1] > ema20.iloc[-1])
    return {
        "roc5_pct": round(roc5, 2),
        "rel_volume": rel_vol,
        "bullish_trend": bullish,
        "last_close": round(float(close.iloc[-1]), 2),
        "data_as_of": str(df.index[-1].date()),
        "data_source": data_source,
    }


def scan_sector_rotation(top_n: int = 3, stocks_per_sector: int = 3) -> dict:
    """
    Rank NSE sector indices by 5-day momentum and relative volume to find
    which sectors are rotating into strength right now, then rank a few
    representative large-cap stocks within the top sectors the same way.
    Also flags sectors that may rotate in NEXT: heavier-than-usual volume
    (rel_volume >= 1.3x) without a confirmed price move yet, returned in
    building_momentum.

    All figures are fetched live from yfinance at call time. Returns
    data_as_of per instrument so the caller can verify freshness (yfinance
    NSE data is EOD/15-min-delayed, not tick-level). Educational
    momentum/volume screen only — not investment advice.
    """
    fetched_at = now_ist_str()
    sector_results = {}
    for name, ticker in SECTOR_INDICES.items():
        try:
            m = _momentum_volume(ticker)
            if m:
                score = round(m["roc5_pct"] * (m["rel_volume"] or 1), 2)
                sector_results[name] = {**m, "score": score}
            else:
                sector_results[name] = {"error": "insufficient data"}
        except Exception as e:
            sector_results[name] = {"error": str(e)}

    ranked_sectors = sorted(
        [(k, v) for k, v in sector_results.items() if "score" in v],
        key=lambda kv: kv[1]["score"], reverse=True,
    )
    top_sectors = ranked_sectors[:top_n]

    # "Building Momentum": heavier-than-usual participation (rel_volume >= 1.3x)
    # without a confirmed price move yet (roc5 still flat/slightly negative).
    # Classic early-accumulation pattern — reclassification of data already
    # fetched above, no extra yfinance calls.
    building_momentum = [
        {"sector": k, **v} for k, v in sector_results.items()
        if "score" in v and v.get("rel_volume") is not None
        and v["rel_volume"] >= 1.3 and -1.0 <= v["roc5_pct"] <= 0.5
    ]
    building_momentum.sort(key=lambda s: s["rel_volume"], reverse=True)

    stock_picks = {}
    for sector_name, _ in top_sectors:
        picks = []
        for tkr in SECTOR_STOCKS.get(sector_name, []):
            try:
                m = _momentum_volume(tkr)
                if m:
                    score = round(m["roc5_pct"] * (m["rel_volume"] or 1), 2)
                    picks.append({"symbol": tkr.replace(".NS", ""), **m, "score": score})
            except Exception:
                continue
        picks.sort(key=lambda p: p["score"], reverse=True)
        stock_picks[sector_name] = picks[:stocks_per_sector]

    # Real aggregate across every sector-index + stock fetch this call actually
    # made (FIXED 07-Jul-2026: this used to hardcode "yfinance" unconditionally
    # even when Kite's live historical API was used for some or all of them —
    # each per-sector/per-stock dict already carries its own real data_source
    # via the **m spread above, this just summarizes them honestly).
    all_sources = [v["data_source"] for v in sector_results.values() if "data_source" in v]
    for picks in stock_picks.values():
        all_sources.extend(p["data_source"] for p in picks if "data_source" in p)
    kite_n = all_sources.count("kite")
    yf_n = all_sources.count("yfinance")
    if all_sources and yf_n == 0:
        source_label = "kite (official, live session)"
    elif all_sources and kite_n == 0:
        source_label = "yfinance (NSE EOD/delayed quotes, fetched live at call time)"
    elif all_sources:
        source_label = f"mixed: {kite_n} kite (live) / {yf_n} yfinance (EOD/delayed) fetches"
    else:
        source_label = "unknown (no fetch returned data_source)"

    return {
        "fetched_at": fetched_at,
        "data_source": source_label,
        "method": "score = 5-day ROC% x relative volume (today's volume / 20-day avg volume). Higher = stronger momentum + heavier-than-usual participation.",
        "all_sectors_ranked": [{"sector": k, **v} for k, v in ranked_sectors],
        "sectors_with_errors": {k: v["error"] for k, v in sector_results.items() if "error" in v},
        "top_sectors": [k for k, _ in top_sectors],
        "building_momentum": building_momentum,
        "stock_picks_by_sector": stock_picks,
        "disclaimer": "Educational momentum/volume screen only, not investment advice. Sector-stock membership list is a static classification — verify current constituents and live price in your broker terminal (e.g. Zerodha Kite) before placing any order.",
    }
