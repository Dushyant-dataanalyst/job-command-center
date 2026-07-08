"""
Cloud-compatible F&O refresh — no FastMCP dependency.
Runs on GitHub Actions every 5 min during market hours.
"""
import sys, os, json, math, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd
import requests
import warnings
from datetime import date, timedelta
from market_data import get_ohlcv
from ist_time import now_ist_str
from alerts import send_alert

warnings.filterwarnings('ignore')

REPO_ROOT = pathlib.Path(__file__).parent.parent
SIDECAR_FILE = REPO_ROOT / "fo_latest.json"
FO_ALERT_STATE_FILE = REPO_ROOT / "logs" / "fo_alert_state.json"

# ── Same constants as fo_traders_mcp.py ───────────────────────────────────
INSTRUMENTS = {
    "NIFTY50":   {"ticker": "^NSEI",    "lot": 75,  "step": 50,  "expiry_day": 3},
    "BANKNIFTY": {"ticker": "^NSEBANK", "lot": 30,  "step": 100, "expiry_day": 2},
}

def _next_monthly_expiry(weekday):
    today = date.today()
    for mo_off in range(0, 3):
        yr = today.year + (today.month + mo_off - 1) // 12
        mo = (today.month + mo_off - 1) % 12 + 1
        last = date(yr + 1, 1, 1) - timedelta(days=1) if mo == 12 else date(yr, mo + 1, 1) - timedelta(days=1)
        d = last
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        if (d - today).days >= 7:
            return d
    # fallback: next weekly
    d = today + timedelta(days=1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d

def _atm(spot, step):
    return int(round(spot / step) * step)

def _premium_estimate(spot, strike, ann_vol, days, option_type="CE"):
    """Simplified premium approximation: intrinsic value (real, grows as the
    option moves ITM) plus a vol-based extrinsic/time-value estimate that
    itself shrinks the further the option sits from the strike in EITHER
    direction (a real option-pricing property — deep ITM/OTM options carry
    little time value).

    BUG FIXED 04-Jul-2026: the "moneyness haircut" below used to discount the
    ENTIRE premium (not just the extrinsic component), which meant an option
    moving further ITM — genuinely good news, intrinsic value rising — could
    show a falling estimated premium instead. Verified against a real trade:
    NIFTY spot moved +0.78% in the favorable direction for an open CE
    position, yet the old formula showed the premium (and P&L) dropping.
    Confirmed the corrected math no longer does this.
    """
    T = max(days, 1) / 365.0
    if option_type == "PE":
        intrinsic = max(0.0, strike - spot)
    else:
        intrinsic = max(0.0, spot - strike)
    mono = abs(spot - strike) / spot
    extrinsic = 0.4 * ann_vol * spot * math.sqrt(T)
    if mono > 0.002:
        extrinsic *= max(0.3, 1 - mono * 8)
    return round(intrinsic + extrinsic, 0)

def _get_indicators(ticker):
    df, data_source = get_ohlcv(ticker, period="6mo")
    if df.empty or len(df) < 55:
        return {}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    close = df['close']; high = df['high']; low = df['low']
    ema9  = close.ewm(span=9,  adjust=False).mean()
    ema18 = close.ewm(span=18, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    tr    = pd.concat([(high-low),(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
    atr   = float(tr.rolling(14).mean().iloc[-1])
    delta = close.diff()
    rsi   = float((100-(100/(1+delta.clip(lower=0).rolling(14).mean()/(-delta.clip(upper=0)).rolling(14).mean().replace(0,np.nan)))).iloc[-1])
    macd  = close.ewm(span=12,adjust=False).mean()-close.ewm(span=26,adjust=False).mean()
    sig   = macd.ewm(span=9,adjust=False).mean()
    ann_vol = float(close.pct_change().iloc[-30:].std()*(252**0.5))
    prev_h,prev_l,prev_c = float(high.iloc[-2]),float(low.iloc[-2]),float(close.iloc[-2])
    pivot = (prev_h+prev_l+prev_c)/3
    return {
        "spot":round(float(close.iloc[-1]),2),
        "ema9":round(float(ema9.iloc[-1]),2),
        "ema18":round(float(ema18.iloc[-1]),2),
        "ema20":round(float(ema20.iloc[-1]),2),
        "ema50":round(float(ema50.iloc[-1]),2),
        "atr":round(atr,2), "rsi":round(rsi,1),
        "macd":round(float(macd.iloc[-1]),2),
        "macd_signal":round(float(sig.iloc[-1]),2),
        "ann_vol":round(ann_vol,4),
        "pivot":round(pivot,2),
        "r1":round(2*pivot-prev_l,2),
        "s1":round(2*pivot-prev_h,2),
        "roc5":round(float(close.pct_change(5).iloc[-1]*100),2),
        "roc3":round(float(close.pct_change(3).iloc[-1]*100),2),
        "data_date":str(df.index[-1].date()),
        "data_source":data_source,
    }

def _signals(v):
    # Simplified 3-factor consensus (no FastMCP needed)
    ce, pe = 0, 0
    if v['spot'] > v['ema18'] > v['ema50']:  ce += 2
    elif v['spot'] < v['ema18'] < v['ema50']: pe += 2
    if v['macd'] > v['macd_signal']:          ce += 1
    else:                                      pe += 1
    if v['spot'] > v['r1']:                   ce += 2
    elif v['spot'] < v['s1']:                 pe += 2
    if v['roc5'] > 1.5:                       ce += 1
    elif v['roc5'] < -1.5:                    pe += 1
    consensus = "BUY_CE" if ce >= 4 else "BUY_PE" if pe >= 4 else "WAIT"
    return consensus, ce, pe


def _signal_commentary(v, ce, pe, consensus):
    """Human-readable breakdown of WHY _signals() returned this consensus --
    purely additive (does not touch _signals()' own vote logic or return
    shape, so backtest_fo.py's direct import of _signals/_atm/_premium_estimate
    stays byte-identical, zero drift risk per its own design). Walks the
    same 4 factors _signals() checks, in the same order, stating what fired
    and what didn't -- shared by index F&O (refresh_fo_cloud._fo_signal)
    and stock F&O (stock_fo_refresh._stock_signal), since both call
    _signals() on the same indicator shape."""
    parts = []
    if v['spot'] > v['ema18'] > v['ema50']:
        parts.append(f"price {v['spot']} is above EMA18 ({v['ema18']}) and EMA50 ({v['ema50']}) -- established uptrend (+2 CE)")
    elif v['spot'] < v['ema18'] < v['ema50']:
        parts.append(f"price {v['spot']} is below EMA18 ({v['ema18']}) and EMA50 ({v['ema50']}) -- established downtrend (+2 PE)")
    else:
        parts.append("EMA18/EMA50 not cleanly stacked with price -- no trend edge from this factor")

    if v['macd'] > v['macd_signal']:
        parts.append(f"MACD ({v['macd']}) is above its signal line ({v['macd_signal']}) -- bullish momentum (+1 CE)")
    else:
        parts.append(f"MACD ({v['macd']}) is below its signal line ({v['macd_signal']}) -- bearish momentum (+1 PE)")

    if v['spot'] > v['r1']:
        parts.append(f"price broke above pivot resistance R1 ({v['r1']}) -- breakout confirmed (+2 CE)")
    elif v['spot'] < v['s1']:
        parts.append(f"price broke below pivot support S1 ({v['s1']}) -- breakdown confirmed (+2 PE)")
    else:
        parts.append(f"price is between pivot support ({v['s1']}) and resistance ({v['r1']}) -- no breakout yet")

    if v['roc5'] > 1.5:
        parts.append(f"5-day momentum (ROC5 {v['roc5']}%) is strongly positive (+1 CE)")
    elif v['roc5'] < -1.5:
        parts.append(f"5-day momentum (ROC5 {v['roc5']}%) is strongly negative (+1 PE)")
    else:
        parts.append(f"5-day momentum (ROC5 {v['roc5']}%) is muted -- no edge from this factor")

    if consensus == "WAIT":
        verdict = f"WAIT (CE {ce}/6, PE {pe}/6 -- neither side reached the 4-vote threshold): "
    else:
        verdict = f"{consensus} ({ce if consensus == 'BUY_CE' else pe}/6 votes): "
    return verdict + " | ".join(parts)

def _fo_signal(instrument):
    cfg = INSTRUMENTS[instrument]
    v   = _get_indicators(cfg["ticker"])
    if not v:
        # yfinance is down — Kite's simple quote API can give a live spot
        # price but not 6 months of historical OHLCV, so it can't
        # reconstruct the EMA/RSI/MACD/ADX signal. Surface the spot price
        # so the dashboard isn't blank, but be explicit that no consensus
        # signal is available rather than fabricating one from a single
        # price point.
        from kite_fallback import get_index_spot
        spot = get_index_spot(instrument)
        if spot is not None:
            return {
                "instrument": instrument, "spot": round(float(spot), 2),
                "consensus": "WAIT", "ce_votes": 0, "pe_votes": 0,
                "data_as_of": None, "fetched_at": now_ist_str(),
                "data_warning": "yfinance unavailable — showing live spot from Kite fallback only. "
                                "No technical signal available (needs historical data yfinance normally provides). "
                                "Verify in Kite before trading.",
                "trade": {"action": "WAIT — No Trade (signal unavailable, yfinance down)"},
            }
        return {"error": f"No data for {instrument} — yfinance and Kite fallback both unavailable"}
    consensus, ce_v, pe_v = _signals(v)
    commentary = _signal_commentary(v, ce_v, pe_v, consensus)
    expiry  = _next_monthly_expiry(cfg["expiry_day"])
    days_to = max((expiry - date.today()).days, 1)
    spot    = v["spot"]
    lot     = cfg["lot"]
    step    = cfg["step"]
    strike  = _atm(spot, step) if consensus != "WAIT" else 0
    opt     = "CE" if consensus == "BUY_CE" else "PE" if consensus == "BUY_PE" else ""
    nse_sym = "NIFTY" if instrument == "NIFTY50" else "BANKNIFTY"
    prem    = _premium_estimate(spot, strike, v["ann_vol"], days_to, opt) if opt else 0
    sl      = round(prem * 0.70, 0)   # exit at 70% of entry = 30% loss
    t1      = round(prem * 1.40, 0)
    t2      = round(prem * 2.00, 0)
    cost    = round(prem * lot, 0)
    zs      = f"{nse_sym}{expiry.strftime('%y%b').upper()}{strike}{opt}" if opt else "—"
    zs_search = f"{nse_sym} {expiry.strftime('%b').upper()} {strike} {opt}" if opt else "—"
    now_str = now_ist_str()
    return {
        "instrument": instrument, "spot": spot, "ann_vol": v["ann_vol"],
        "consensus": consensus, "ce_votes": ce_v, "pe_votes": pe_v,
        "commentary": commentary,
        "data_as_of": v["data_date"], "fetched_at": now_str,
        "data_source": v["data_source"],
        "data_warning": (
            f"Signals based on {v['data_date']} Kite historical data (official, live session) — "
            "still end-of-day candles, not tick-level. Verify in Kite before trading."
            if v["data_source"] == "kite" else
            f"Signals based on {v['data_date']} close (yfinance EOD, delayed/unofficial). Verify in Kite before trading."
        ),
        "trade": {
            "action": f"BUY {nse_sym} {strike} {opt}" if opt else "WAIT — No Trade",
            "zerodha_symbol": zs, "zerodha_search": zs_search,
            "strike": strike, "expiry": expiry.strftime("%d %b %Y"),
            "days_to_exp": days_to, "lot_size": lot,
            "entry_premium": prem, "sl_premium": sl,
            "target1_premium": t1, "target_premium": t2,
            "cost_1_lot": cost,
        }
    }

def _load_fo_alert_state():
    if not FO_ALERT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(FO_ALERT_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_fo_alert_state(state):
    FO_ALERT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    FO_ALERT_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _alert_fo_signal_changes(fo_exact):
    """This refresh runs every 5 min — alert only on a NEW actionable signal
    (WAIT -> BUY_CE/BUY_PE, or a direction flip), not every run while the
    same signal still holds, so it doesn't spam every 5 minutes. State is
    persisted in logs/fo_alert_state.json so a restart/redeploy doesn't
    accidentally re-fire on an already-alerted, still-active signal."""
    state = _load_fo_alert_state()
    for inst, r in fo_exact.items():
        if inst == "_meta" or not isinstance(r, dict) or "error" in r:
            continue
        consensus = r.get("consensus")
        prev = state.get(inst)
        if consensus and consensus != "WAIT" and consensus != prev:
            trade = r.get("trade", {})
            votes = r.get("ce_votes") if consensus == "BUY_CE" else r.get("pe_votes")
            text = (
                f"{inst}: {trade.get('action', consensus)} — spot {r.get('spot')}, "
                f"signal score {votes}/6, entry premium ~{trade.get('entry_premium')}, "
                f"SL {trade.get('sl_premium')}, T1 {trade.get('target1_premium')}"
            )
            send_alert(text, level="SIGNAL")
        state[inst] = consensus
    _save_fo_alert_state(state)


def main():
    now_str = now_ist_str()
    print(f"[{now_str}] Cloud F&O refresh starting...")
    fo_exact = {}
    sources_used = []
    for inst in ["NIFTY50", "BANKNIFTY"]:
        try:
            r = _fo_signal(inst)
            fo_exact[inst] = r
            if r.get("data_source"):
                sources_used.append(r["data_source"])
            print(f"  {inst}: {r.get('consensus')} | spot={r.get('spot')} | source={r.get('data_source', '?')}")
        except Exception as e:
            print(f"  {inst} ERROR: {e}")
    # Real aggregate, not a hardcoded guess -- built AFTER the loop so it
    # reflects what each instrument actually used this run (FIXED 07-Jul-2026:
    # this used to hardcode "yfinance EOD" unconditionally even on runs where
    # Kite's live historical API was actually used for both instruments).
    if sources_used and all(s == "kite" for s in sources_used):
        source_label = "kite (official, live session)"
    elif sources_used and all(s == "yfinance" for s in sources_used):
        source_label = "yfinance EOD (delayed/unofficial)"
    elif sources_used:
        source_label = "mixed: " + ", ".join(f"{inst}={fo_exact[inst].get('data_source', '?')}" for inst in fo_exact if fo_exact[inst].get("data_source"))
    else:
        source_label = "unknown (no instrument returned data_source)"
    fo_exact["_meta"] = {"generated_at": now_str, "source": source_label}
    # Write the sidecar that the dashboard fetches at /fo_latest.json (repo root = Vercel site root)
    SIDECAR_FILE.write_text(json.dumps(fo_exact, indent=2), encoding="utf-8")
    print(f"  Wrote {SIDECAR_FILE}")
    _alert_fo_signal_changes(fo_exact)
    print("Done.")

if __name__ == "__main__":
    main()
