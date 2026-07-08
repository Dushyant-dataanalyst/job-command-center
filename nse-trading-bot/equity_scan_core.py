"""
Core equity-scan logic: implements the 4 named strategies (Inna, Pham,
Cianni, Unger) that drive the dashboard's Equity BUY tab, replacing what
was previously a hand-pasted SCAN_DATA snapshot in the HTML (last updated
25 Jun 2026, never refreshed since).

IMPORTANT — reconstruction disclaimer: the original strategy logic was
never documented anywhere in this repo beyond one-line tooltip hints
(nse_live_dashboard.html's patchStrategyTooltips()):
  Inna:   "18-MA pullback in uptrend — bounce off MA18 with volume"
  Pham:   "RSI recovery + EMA9>20>50 stack, RSI 35-60, PEG<2.5"
  Cianni: "20/50-day breakout with ADX>22 + volume surge"
  Unger:  "3 sub-systems vote (breakout/trend/mean-reversion) — 2 of 3 must trigger"
Everything below is a best-effort, technically-sound implementation of
those descriptions using standard indicator math — not a verified match
to an original spec. Treat signals as an educational screen, same
disclaimer posture as sector_rotation_core.py and refresh_fo_cloud.py.

Universe: reuses SECTOR_STOCKS from sector_rotation_core.py (46 stocks /
10 sectors, already verified-working tickers) rather than introducing a
third separate stock list — the original hand-pasted SCAN_DATA covered a
slightly different 43-stock set with no documented membership rule.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

from market_data import get_ohlcv
from macro_gate import load_macro_risk, macro_context

REPO_ROOT = pathlib.Path(__file__).parent.parent
VOTER_WEIGHTS_FILE = REPO_ROOT / "voter_weights.json"
from sector_rotation_core import SECTOR_STOCKS

STRATEGY_NAMES = ["Inna", "Pham", "Cianni", "Unger"]


def _ticker_sector_map():
    m = {}
    for sector, tickers in SECTOR_STOCKS.items():
        for t in tickers:
            m[t.replace(".NS", "")] = sector
    return m


def _adx14(high, low, close):
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / 14, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / 14, adjust=False).mean()
    return adx


def _peg_ratio(ticker):
    """Best-effort fundamental lookup — yfinance .info is slow/flaky, so this
    degrades to None (treated as 'unknown, don't block on it') rather than
    retrying or failing the whole scan for one stock's fundamentals."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        peg = info.get("trailingPegRatio") or info.get("pegRatio")
        return float(peg) if peg is not None else None
    except Exception:
        return None


def _extended_indicators(ticker):
    df, data_source = get_ohlcv(ticker, period="6mo")
    if df.empty or len(df) < 55:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema18 = close.ewm(span=18, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1])
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean().replace(0, np.nan)
    rsi = 100 - (100 / (1 + gain / loss))
    adx = _adx14(high, low, close)

    high20_prev = high.rolling(20).max().shift(1)
    high50_prev = high.rolling(50).max().shift(1)
    low10_prev = low.rolling(10).min().shift(1)

    vol_nonzero = vol[vol > 0]
    rel_volume = None
    if len(vol_nonzero) >= 21:
        last_vol = float(vol_nonzero.iloc[-1])
        avg_vol20 = float(vol_nonzero.iloc[-21:-1].mean())
        rel_volume = round(last_vol / avg_vol20, 2) if avg_vol20 else None

    return {
        "spot": float(close.iloc[-1]),
        "ema9": float(ema9.iloc[-1]), "ema18": float(ema18.iloc[-1]),
        "ema20": float(ema20.iloc[-1]), "ema50": float(ema50.iloc[-1]),
        "atr": atr,
        "rsi": float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
        "rsi_3d_ago": float(rsi.iloc[-4]) if len(rsi) > 4 and pd.notna(rsi.iloc[-4]) else None,
        "adx": float(adx.iloc[-1]) if pd.notna(adx.iloc[-1]) else None,
        "high20_prev": float(high20_prev.iloc[-1]) if pd.notna(high20_prev.iloc[-1]) else None,
        "high50_prev": float(high50_prev.iloc[-1]) if pd.notna(high50_prev.iloc[-1]) else None,
        "low10_prev": float(low10_prev.iloc[-1]) if pd.notna(low10_prev.iloc[-1]) else None,
        "low_recent_min": float(low.iloc[-3:].min()),
        "prev_close": float(close.iloc[-2]),
        "rel_volume": rel_volume,
        "data_date": str(df.index[-1].date()),
        "data_source": data_source,
    }


def _strategy_inna(v):
    """18-MA pullback in uptrend — bounce off MA18 with volume."""
    uptrend = v["spot"] > v["ema50"]
    touched = v["low_recent_min"] <= v["ema18"] * 1.01
    bounced = v["spot"] > v["ema18"] and v["spot"] > v["prev_close"]
    vol_confirm = (v["rel_volume"] or 0) >= 1.3
    if uptrend and touched and bounced:
        return "STRONG_BUY" if vol_confirm else "BUY"
    if uptrend and touched:
        return "WATCH"
    return "NO_SIGNAL"


def _strategy_pham(v, peg):
    """RSI recovery + EMA9>20>50 stack, RSI 35-60, PEG<2.5."""
    ema_stack = v["ema9"] > v["ema20"] > v["ema50"]
    rsi = v["rsi"]
    rsi_recovering = (
        rsi is not None and v["rsi_3d_ago"] is not None
        and 35 <= rsi <= 60 and rsi > v["rsi_3d_ago"]
    )
    peg_ok = peg is None or peg < 2.5
    if ema_stack and rsi_recovering:
        return "STRONG_BUY" if (peg is not None and peg < 1.5) else ("BUY" if peg_ok else "WATCH")
    if ema_stack or rsi_recovering:
        return "WATCH"
    return "NO_SIGNAL"


def _strategy_cianni(v):
    """20/50-day breakout with ADX>22 + volume surge."""
    breakout20 = v["high20_prev"] is not None and v["spot"] > v["high20_prev"]
    breakout50 = v["high50_prev"] is not None and v["spot"] > v["high50_prev"]
    adx_strong = (v["adx"] or 0) >= 22
    vol_surge = (v["rel_volume"] or 0) >= 1.3
    if (breakout20 or breakout50) and adx_strong and vol_surge:
        return "STRONG_BUY" if breakout50 else "BUY"
    if (breakout20 or breakout50) and adx_strong:
        return "BUY"
    if breakout20 or adx_strong:
        return "WATCH"
    return "NO_SIGNAL"


def _strategy_unger(v):
    """3 sub-systems vote (breakout/trend/mean-reversion) — 2 of 3 must trigger."""
    sub_breakout = v["high20_prev"] is not None and v["spot"] > v["high20_prev"]
    sub_trend = v["ema9"] > v["ema18"] > v["ema50"]
    rsi = v["rsi"]
    sub_meanrev = (
        v["low10_prev"] is not None and v["low_recent_min"] <= v["low10_prev"] * 1.01
        and v["spot"] > v["prev_close"]
    ) or (rsi is not None and rsi < 35 and v["rsi_3d_ago"] is not None and rsi > v["rsi_3d_ago"])
    votes = sum([sub_breakout, sub_trend, sub_meanrev])
    if votes >= 3:
        return "STRONG_BUY"
    if votes == 2:
        return "BUY"
    if votes == 1:
        return "WATCH"
    return "NO_SIGNAL"


def _voter_commentary(v, peg):
    """Human-readable one-liner per voter explaining WHY it voted the way
    it did -- purely additive, does not touch _strategy_*()'s own logic or
    return shape (backtest.py imports those 4 functions directly for
    zero-drift replay of the live logic; this must never affect them).
    Walks the exact same conditions each _strategy_*() checks, in the same
    order, so a discrepancy between the vote and this text would mean a
    copy-paste bug here, not a second scoring system."""
    rsi = v["rsi"]
    c = {}

    uptrend = v["spot"] > v["ema50"]
    touched = v["low_recent_min"] <= v["ema18"] * 1.01
    bounced = v["spot"] > v["ema18"] and v["spot"] > v["prev_close"]
    vol_confirm = (v["rel_volume"] or 0) >= 1.3
    if uptrend and touched and bounced:
        c["Inna"] = (f"Price pulled back to EMA18 ({v['ema18']:.2f}) within an uptrend (above EMA50 {v['ema50']:.2f}) and bounced"
                     + (f", with volume {v['rel_volume']}x average confirming it -- high-conviction bounce" if vol_confirm
                        else f", but volume is only {v['rel_volume']}x average -- lighter conviction"))
    elif uptrend and touched:
        c["Inna"] = f"Price touched EMA18 ({v['ema18']:.2f}) within an uptrend but hasn't bounced back above it yet -- watching for confirmation"
    elif uptrend:
        c["Inna"] = f"In an uptrend (above EMA50 {v['ema50']:.2f}) but hasn't pulled back to EMA18 ({v['ema18']:.2f}) yet -- no setup"
    else:
        c["Inna"] = f"Not in an uptrend (price below EMA50 {v['ema50']:.2f}) -- this pullback strategy needs an uptrend first"

    ema_stack = v["ema9"] > v["ema20"] > v["ema50"]
    rsi_recovering = rsi is not None and v["rsi_3d_ago"] is not None and 35 <= rsi <= 60 and rsi > v["rsi_3d_ago"]
    if ema_stack and rsi_recovering:
        peg_txt = (f", PEG {peg} is attractive" if (peg is not None and peg < 1.5)
                   else f", PEG {peg} is rich -- capped at WATCH" if (peg is not None and peg >= 2.5)
                   else f", PEG {peg} is acceptable" if peg is not None else "")
        c["Pham"] = f"EMA9>EMA20>EMA50 stack confirms uptrend and RSI ({rsi:.1f}) is recovering from {v['rsi_3d_ago']:.1f} through the 35-60 sweet spot{peg_txt}"
    elif ema_stack or rsi_recovering:
        c["Pham"] = ("EMA9>EMA20>EMA50 stack is bullish but RSI isn't recovering through the 35-60 band yet" if ema_stack
                     else f"RSI ({rsi:.1f}) is recovering but the EMA9/20/50 stack isn't confirmed yet")
    else:
        c["Pham"] = "Neither the EMA stack nor the RSI-recovery condition is met -- no setup"

    breakout20 = v["high20_prev"] is not None and v["spot"] > v["high20_prev"]
    breakout50 = v["high50_prev"] is not None and v["spot"] > v["high50_prev"]
    adx = v["adx"] or 0
    adx_strong = adx >= 22
    vol_surge = (v["rel_volume"] or 0) >= 1.3
    if (breakout20 or breakout50) and adx_strong and vol_surge:
        c["Cianni"] = f"Broke above its {'50' if breakout50 else '20'}-day high with ADX {adx:.1f} confirming trend strength and {v['rel_volume']}x volume surge backing it"
    elif (breakout20 or breakout50) and adx_strong:
        c["Cianni"] = f"Broke above its {'50' if breakout50 else '20'}-day high with ADX {adx:.1f} confirming trend strength, but volume ({v['rel_volume']}x) hasn't surged to back it"
    elif breakout20 or adx_strong:
        c["Cianni"] = (f"Broke above its 20-day high but ADX ({adx:.1f}) isn't strong enough yet (needs 22+)" if breakout20
                       else f"ADX ({adx:.1f}) shows trend strength but price hasn't broken its 20/50-day high yet")
    else:
        c["Cianni"] = f"No breakout and ADX ({adx:.1f}) is weak -- no setup"

    sub_breakout = v["high20_prev"] is not None and v["spot"] > v["high20_prev"]
    sub_trend = v["ema9"] > v["ema18"] > v["ema50"]
    sub_meanrev = (
        v["low10_prev"] is not None and v["low_recent_min"] <= v["low10_prev"] * 1.01 and v["spot"] > v["prev_close"]
    ) or (rsi is not None and rsi < 35 and v["rsi_3d_ago"] is not None and rsi > v["rsi_3d_ago"])
    fired = [name for name, ok in [("breakout", sub_breakout), ("trend", sub_trend), ("mean-reversion", sub_meanrev)] if ok]
    c["Unger"] = ((f"{len(fired)}/3 sub-systems agree ({', '.join(fired)})" if fired else "0/3 sub-systems agree")
                  + " -- needs 2/3 for a BUY, 3/3 for STRONG_BUY")

    # Not conditional -- ALWAYS true: all 4 voters are computed from the same
    # single 6mo OHLCV pull (_extended_indicators() fetches it once per
    # stock). Inna's uptrend, Pham's EMA stack, Cianni's breakout context,
    # and Unger's sub_trend all substantially overlap on "price above its
    # short/medium moving averages" -- a 4/4 agreement is one trend read 4
    # ways, not 4 independent opinions. "_note" is not a voter name (never
    # indexed by the dashboard's per-badge lookup, which uses explicit voter
    # names) -- it's a standalone, always-present disclaimer key.
    c["_note"] = ("⚠ All 4 voters score the same single price series -- not independent confirmations. "
                  "A 4/4 STRONG_BUY means one strong trend measured 4 ways, not 4 separate signals agreeing.")

    return c


_VOTER_WEIGHTS_CACHE = None


def _load_voter_weights():
    """Cached per-process — this gets called once per stock in a 49-stock
    scan, no reason to re-read the file every time. Defaults to equal
    weight (0.25 each) if voter_weights.json is missing/malformed, same
    fallback voter_weights_refresh.py itself uses when there isn't enough
    closed-trade history yet."""
    global _VOTER_WEIGHTS_CACHE
    if _VOTER_WEIGHTS_CACHE is not None:
        return _VOTER_WEIGHTS_CACHE
    equal = {"Inna": 0.25, "Pham": 0.25, "Cianni": 0.25, "Unger": 0.25}
    if not VOTER_WEIGHTS_FILE.exists():
        _VOTER_WEIGHTS_CACHE = equal
        return equal
    try:
        data = json.loads(VOTER_WEIGHTS_FILE.read_text(encoding="utf-8"))
        voters = data.get("voters", {})
        weights = {name: voters.get(name, {}).get("weight", 0.25) for name in equal}
        _VOTER_WEIGHTS_CACHE = weights
        return weights
    except Exception:
        _VOTER_WEIGHTS_CACHE = equal
        return equal


def scan_one(symbol, sector, fetch_peg=True, macro=None):
    """Returns None if there wasn't enough price history to score this
    stock — caller should skip it, not treat it as an error.

    macro_context (added 08-Jul-2026): equity was the ONLY signal type in
    this entire system with zero macro-risk awareness at any layer -- index
    F&O gates trade_brain.py opens and expert_gate.py's CONFIRMED_ENTRY,
    stock F&O flags every signal, equity flagged nothing, ever. There's no
    automated equity trade-opening code to gate (manual "I bought" only),
    so this FLAGS like stock F&O does, never suppresses the signal -- always
    attached, even when macro is calm, so the current backdrop is visible
    next to every signal, not just conditionally on a crisis day.

    sector_macro_flag (added 08-Jul-2026): macro_risk.json's avoid_sectors/
    watch_sectors were already computed but never cross-referenced against
    any equity signal's own sector -- a generic "macro is EXTREME" banner
    doesn't tell you WHICH stocks that actually touches. Exact-string match
    only; macro_risk_refresh.py's sector taxonomy (e.g. Aviation, OMC,
    Defence) only partially overlaps this 10-sector equity universe (e.g.
    Auto, Energy do match) -- a stock in a sector macro doesn't track at all
    correctly gets no flag, not a guessed one."""
    ticker = symbol + ".NS"
    v = _extended_indicators(ticker)
    if v is None:
        return None
    peg = _peg_ratio(ticker) if fetch_peg else None
    if macro is None:
        macro = load_macro_risk()

    strategies = {
        "Inna": _strategy_inna(v),
        "Pham": _strategy_pham(v, peg),
        "Cianni": _strategy_cianni(v),
        "Unger": _strategy_unger(v),
    }
    buy_votes = sum(1 for s in strategies.values() if s in ("BUY", "STRONG_BUY"))

    # Weighted consensus: each voter's weight is scaled by 4 (the voter
    # count) so equal weights (0.25 each, the default until enough real
    # closed-trade history exists) reproduce the exact plain vote count —
    # zero behavior change on day one. As voter_weights_refresh.py learns
    # real win rates over time, a consistently-right voter's YES vote counts
    # for more than 1, and a consistently-wrong voter's for less, without
    # needing a whole new threshold system.
    weights = _load_voter_weights()
    weighted_votes = round(sum(
        weights.get(name, 0.25) * 4
        for name, sig in strategies.items() if sig in ("BUY", "STRONG_BUY")
    ), 2)
    signal = "STRONG_BUY" if weighted_votes >= 3 else "BUY" if weighted_votes >= 2 else "WATCH"

    entry = round(v["spot"], 2)
    atr = v["atr"] or entry * 0.02
    sl = round(entry - 1.5 * atr, 2)
    risk = max(entry - sl, 0.01)
    t1 = round(entry + 1.25 * risk, 2)
    t2 = round(entry + 2.5 * risk, 2)
    t3 = round(entry + 3.75 * risk, 2)
    pct_t1 = round((t1 - entry) / entry * 100, 1) if buy_votes >= 1 else 0

    adj = (macro or {}).get("trade_adjustments") or {}
    sector_macro_flag = (
        "avoid" if sector in (adj.get("avoid_sectors") or []) else
        "watch" if sector in (adj.get("watch_sectors") or []) else
        None
    )

    return {
        "signal": signal,
        "buy_votes": buy_votes,
        "weighted_votes": weighted_votes,
        "entry": entry, "sl": sl, "t1": t1, "t2": t2, "t3": t3,
        "pct_t1": pct_t1,
        "strategies": strategies,
        "commentary": _voter_commentary(v, peg),
        "sector": sector,
        "data_as_of": v["data_date"],
        "data_source": v["data_source"],
        "macro_context": macro_context(macro),
        "sector_macro_flag": sector_macro_flag,
    }


def scan_universe(fetch_peg=True):
    sector_map = _ticker_sector_map()
    results = {}
    errors = {}
    macro = load_macro_risk()  # loaded once per run, not once per stock -- matches _load_voter_weights()'s per-process caching
    for symbol, sector in sector_map.items():
        try:
            r = scan_one(symbol, sector, fetch_peg=fetch_peg, macro=macro)
            if r is not None:
                results[symbol] = r
            else:
                errors[symbol] = "insufficient price history"
        except Exception as e:
            errors[symbol] = str(e)
    return results, errors
