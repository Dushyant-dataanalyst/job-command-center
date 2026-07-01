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
from yf_retry import download_with_retry
from ist_time import now_ist_str

warnings.filterwarnings('ignore')

REPO_ROOT = pathlib.Path(__file__).parent.parent
SIDECAR_FILE = REPO_ROOT / "fo_latest.json"

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

def _premium_estimate(spot, strike, ann_vol, days):
    T = max(days, 1) / 365.0
    mono = abs(spot - strike) / spot
    p = 0.4 * ann_vol * spot * math.sqrt(T)
    if mono > 0.002:
        p *= max(0.3, 1 - mono * 8)
    return round(p, 0)

def _get_indicators(ticker):
    df = download_with_retry(ticker, period="6mo")
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
    expiry  = _next_monthly_expiry(cfg["expiry_day"])
    days_to = max((expiry - date.today()).days, 1)
    spot    = v["spot"]
    lot     = cfg["lot"]
    step    = cfg["step"]
    strike  = _atm(spot, step) if consensus != "WAIT" else 0
    opt     = "CE" if consensus == "BUY_CE" else "PE" if consensus == "BUY_PE" else ""
    nse_sym = "NIFTY" if instrument == "NIFTY50" else "BANKNIFTY"
    prem    = _premium_estimate(spot, strike, v["ann_vol"], days_to) if opt else 0
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
        "data_as_of": v["data_date"], "fetched_at": now_str,
        "data_warning": f"Signals based on {v['data_date']} close (yfinance EOD). Verify in Kite before trading.",
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

def main():
    now_str = now_ist_str()
    print(f"[{now_str}] Cloud F&O refresh starting...")
    fo_exact = {"_meta": {"generated_at": now_str, "source": "yfinance EOD"}}
    for inst in ["NIFTY50", "BANKNIFTY"]:
        try:
            r = _fo_signal(inst)
            fo_exact[inst] = r
            print(f"  {inst}: {r.get('consensus')} | spot={r.get('spot')}")
        except Exception as e:
            print(f"  {inst} ERROR: {e}")
    # Write the sidecar that the dashboard fetches at /fo_latest.json (repo root = Vercel site root)
    SIDECAR_FILE.write_text(json.dumps(fo_exact, indent=2), encoding="utf-8")
    print(f"  Wrote {SIDECAR_FILE}")
    print("Done.")

if __name__ == "__main__":
    main()
