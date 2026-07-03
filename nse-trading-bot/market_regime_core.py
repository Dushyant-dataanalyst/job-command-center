"""
Market regime detection for NIFTY/BANKNIFTY — classifies current trend
direction, trend strength, volatility level, and volume behavior, then
recommends which of the 4 existing equity strategies (Inna/Pham/Cianni/
Unger — see equity_scan_core.py) fit the current regime and which to
avoid right now.

All classifications are computed from the same yfinance OHLCV data already
used elsewhere in this codebase (refresh_fo_cloud.py, equity_scan_core.py)
— no new data source, no fabricated numbers. Volatility level is ranked
against the stock's OWN trailing 6-month ATR distribution (percentile),
not a fixed magic-number threshold, so it self-calibrates per instrument
instead of assuming one universal "normal" volatility band.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

from market_data import get_ohlcv
from equity_scan_core import _adx14

INSTRUMENTS = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}

TREND_ADX_THRESHOLD = 20  # ADX below this = no reliable trend direction (choppy)


def _regime_indicators(ticker):
    df, data_source = get_ohlcv(ticker, period="6mo")
    if df.empty or len(df) < 55:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    ema18 = close.ewm(span=18, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_series = tr.rolling(14).mean()
    atr_pct_series = (atr_series / close * 100).dropna()
    adx = _adx14(high, low, close)

    vol_nonzero = vol[vol > 0]
    rel_volume = None
    if len(vol_nonzero) >= 21:
        last_vol = float(vol_nonzero.iloc[-1])
        avg_vol20 = float(vol_nonzero.iloc[-21:-1].mean())
        rel_volume = round(last_vol / avg_vol20, 2) if avg_vol20 else None

    cur_atr_pct = float(atr_pct_series.iloc[-1])
    percentile = float((atr_pct_series < cur_atr_pct).mean() * 100)  # own trailing 6mo distribution

    return {
        "spot": float(close.iloc[-1]),
        "ema18": float(ema18.iloc[-1]),
        "ema50": float(ema50.iloc[-1]),
        "adx": float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else None,
        "atr_pct": round(cur_atr_pct, 2),
        "atr_percentile": round(percentile, 0),
        "rel_volume": rel_volume,
        "data_date": str(df.index[-1].date()),
        "data_source": data_source,
    }


def _classify_trend(v):
    adx = v["adx"] or 0
    if adx < TREND_ADX_THRESHOLD:
        return "Sideways/Choppy"
    if v["spot"] > v["ema18"] > v["ema50"]:
        return "Bullish"
    if v["spot"] < v["ema18"] < v["ema50"]:
        return "Bearish"
    return "Sideways/Choppy"  # trending ADX but EMA stack not aligned — mixed signal, treat as choppy


def _classify_volatility(v):
    p = v["atr_percentile"]
    if p < 33:
        return "Low"
    if p < 67:
        return "Normal"
    return "High"


def _classify_volume(v):
    rv = v["rel_volume"]
    if rv is None:
        return "Unknown"
    if rv >= 1.2:
        return "Increasing"
    if rv <= 0.8:
        return "Decreasing"
    return "Normal"


def _recommend(regimes):
    """Blends both instruments' regimes into one recommendation — NIFTY and
    BANKNIFTY usually agree (BANKNIFTY is NIFTY's largest sector weight) but
    can diverge; when they do, this favors the lower-conviction (more
    cautious) read rather than picking one arbitrarily."""
    trends = [r["trend"] for r in regimes.values()]
    vols = [r["volatility"] for r in regimes.values()]
    vol_behaviors = [r["volume_behavior"] for r in regimes.values()]

    bullish = trends.count("Bullish") >= 1 and "Bearish" not in trends
    bearish = trends.count("Bearish") >= 1 and "Bullish" not in trends
    choppy = all(t == "Sideways/Choppy" for t in trends)
    high_vol = "High" in vols
    increasing_volume = vol_behaviors.count("Increasing") >= 1

    best_fit, avoid, reasons = [], [], []

    if bullish and not choppy:
        best_fit += ["Inna", "Pham"]
        reasons.append("Bullish trend detected — pullback (Inna) and RSI-recovery (Pham) setups look for continuation entries, which need an established uptrend to work.")
    elif bearish:
        avoid += ["Inna", "Pham"]
        reasons.append("Bearish trend — Inna/Pham are long-only pullback/recovery setups; entries against a downtrend historically underperform.")
    elif choppy:
        avoid.append("Cianni")
        reasons.append("Sideways/choppy market (low ADX) — breakout setups (Cianni) are prone to false breakouts without a real trend to follow through.")

    if increasing_volume and not choppy:
        best_fit.append("Cianni")
        reasons.append("Volume picking up alongside a directional trend — breakout confirmation (Cianni) is more reliable when backed by real participation.")
    elif not increasing_volume and "Cianni" not in avoid:
        reasons.append("Volume isn't confirming a breakout right now — Cianni signals should be treated cautiously until volume picks up.")

    if high_vol:
        reasons.append("Volatility is elevated vs the last 6 months — widen stops or reduce position size regardless of which strategy you're following.")

    best_fit.append("Unger")  # multi-subsystem vote — reasonably adaptive across regimes, always listed
    reasons.append("Unger's 2-of-3 subsystem vote blends breakout/trend/mean-reversion, making it the most regime-agnostic of the four — still worth a sanity check against the above before acting.")

    return {
        "best_fit_strategies": sorted(set(best_fit)),
        "avoid": sorted(set(avoid) - set(best_fit)),
        "reasoning": reasons,
    }


def detect_regime():
    regimes = {}
    errors = {}
    for name, ticker in INSTRUMENTS.items():
        try:
            v = _regime_indicators(ticker)
            if v is None:
                errors[name] = "insufficient price history"
                continue
            regimes[name] = {
                "trend": _classify_trend(v),
                "adx": round(v["adx"], 1) if v["adx"] is not None else None,
                "volatility": _classify_volatility(v),
                "atr_pct": v["atr_pct"],
                "atr_percentile_6mo": v["atr_percentile"],
                "volume_behavior": _classify_volume(v),
                "rel_volume": v["rel_volume"],
                "spot": round(v["spot"], 2),
                "data_as_of": v["data_date"],
                "data_source": v["data_source"],
            }
        except Exception as e:
            errors[name] = str(e)

    recommendation = _recommend(regimes) if regimes else {"best_fit_strategies": [], "avoid": [], "reasoning": ["No regime data available."]}

    return {
        "instruments": regimes,
        "errors": errors,
        "recommendation": recommendation,
        "method": "Trend = EMA18/EMA50 stack direction, gated by ADX>=20 (below that = Sideways/Choppy regardless of EMA stack). "
                  "Volatility = current ATR% ranked against its own trailing 6-month distribution (percentile), not a fixed threshold. "
                  "Volume behavior = today's volume vs 20-day average.",
        "disclaimer": "Educational regime classification only, not investment advice. Recommendation logic is a heuristic mapping from regime to strategy fit — not a verified backtest of which strategy performs best in which regime.",
    }
