"""
Stock F&O Signals — live strike/premium/expiry for individual stock options.

Replaces a static HTML table (frozen at a 25-Jun snapshot) that showed a
"Score" column from a methodology not present anywhere in this codebase
(likely hand-generated externally, same situation as the dashboard's
SCAN_DATA). Rather than guess at reproducing an unknown scoring system,
this reuses the SAME transparent 3-factor consensus already proven for
NIFTY/BANKNIFTY in refresh_fo_cloud.py — documented, not invented.

Strategy selection (a documented rule, not a guess):
  - ce_votes/pe_votes == 6 (max conviction, all 4 factors agree): single-leg
    BUY CE/PE — full premium exposure for the highest-confidence setups.
  - ce_votes/pe_votes in [4, 5] (actionable but not max): a capped-risk
    spread — Bull Call Spread (long ATM CE + short OTM CE) for bullish,
    Bear Put Spread (long ATM PE + short OTM PE) for bearish.

KNOWN LIMITATION — strike intervals: NSE sets per-stock strike intervals
that vary by price band and aren't available from any source in this repo.
_strike_step() below is a documented heuristic approximation, not official
exchange data. Every output is labeled accordingly — verify the actual
listed strikes in your broker's option chain before trading.

KNOWN LIMITATION — no lot size: NSE F&O lot sizes are revised periodically
and aren't available from any live source here, so cost-per-lot is
intentionally omitted (not guessed). All premium/SL/target figures are in
₹-per-share and %, not total position cost.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date

from ist_time import now_ist_str
from refresh_fo_cloud import _get_indicators, _signals, _premium_estimate, _next_monthly_expiry

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "stock_fo.json"

# Large-cap, confirmed-liquid F&O stocks from the equity universe this
# replaces. The static table it replaces had 23 stocks; 6 smaller/mid-cap
# names (CLEAN, GPPL, HONAUT, NAUKRI, PERSISTENT, ROUTE) are deliberately
# excluded — their NSE F&O eligibility can't be confirmed from any source
# in this repo, and fabricating options-strategy suggestions for stocks
# that may not have listed options would be actively misleading.
TRACKED_STOCKS = [
    "ICICIBANK", "AXISBANK", "LT", "SUNPHARMA", "BAJFINANCE",
    "TITAN", "KOTAKBANK", "MARUTI", "SBIN", "HDFCBANK",
    "RELIANCE", "TCS", "INFY", "ITC", "WIPRO", "BHARTIARTL",
    "HINDUNILVR", "NESTLEIND",
]

MONTHLY_EXPIRY_WEEKDAY = 3  # Thursday — same monthly convention as NIFTY


def _strike_step(price):
    """Documented heuristic approximation of NSE strike intervals — NOT
    official exchange data. See module docstring."""
    if price < 100: return 5
    if price < 250: return 10
    if price < 500: return 10
    if price < 1000: return 20
    if price < 2500: return 50
    return 100


def _atm(spot, step):
    return int(round(spot / step) * step)


def _single_leg(spot, strike, opt, ann_vol, days):
    prem = _premium_estimate(spot, strike, ann_vol, days)
    return {
        "type": "single_leg",
        "action": f"BUY {strike} {opt}",
        "strike": strike,
        "entry_premium": prem,
        "sl_premium": round(prem * 0.70, 1),
        "target1_premium": round(prem * 1.40, 1),
        "target2_premium": round(prem * 2.00, 1),
    }


def _spread(spot, atm_strike, otm_strike, opt, ann_vol, days):
    long_prem = _premium_estimate(spot, atm_strike, ann_vol, days)
    short_prem = _premium_estimate(spot, otm_strike, ann_vol, days)
    net_debit = round(long_prem - short_prem, 1)
    width = abs(otm_strike - atm_strike)
    max_profit = round(width - net_debit, 1)
    max_loss = net_debit
    label = "Bull Call Spread" if opt == "CE" else "Bear Put Spread"
    return {
        "type": "spread",
        "action": f"{label}: BUY {atm_strike}{opt} + SELL {otm_strike}{opt}",
        "long_strike": atm_strike, "short_strike": otm_strike,
        "long_premium": long_prem, "short_premium": short_prem,
        "net_debit": net_debit,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "breakeven": round(atm_strike + net_debit, 1) if opt == "CE" else round(atm_strike - net_debit, 1),
    }


def _stock_signal(name):
    v = _get_indicators(name + ".NS")
    if not v:
        return {"error": f"No data for {name}"}
    consensus, ce_v, pe_v = _signals(v)
    spot = v["spot"]
    now_str = now_ist_str()

    if consensus == "WAIT":
        return {
            "name": name, "spot": spot, "consensus": "WAIT",
            "ce_votes": ce_v, "pe_votes": pe_v,
            "data_as_of": v["data_date"], "fetched_at": now_str,
            "data_warning": f"Signals based on {v['data_date']} close (yfinance EOD). Verify in Kite before trading.",
            "trade": None,
        }

    opt = "CE" if consensus == "BUY_CE" else "PE"
    votes = ce_v if consensus == "BUY_CE" else pe_v
    expiry = _next_monthly_expiry(MONTHLY_EXPIRY_WEEKDAY)
    days_to = max((expiry - date.today()).days, 1)
    step = _strike_step(spot)
    atm = _atm(spot, step)

    if votes >= 6:
        trade = _single_leg(spot, atm, opt, v["ann_vol"], days_to)
    else:
        otm = atm + 2 * step if opt == "CE" else atm - 2 * step
        trade = _spread(spot, atm, otm, opt, v["ann_vol"], days_to)

    trade["expiry"] = expiry.strftime("%d %b %Y")
    trade["days_to_exp"] = days_to
    trade["strike_step_used"] = step

    return {
        "name": name, "spot": spot, "consensus": consensus,
        "ce_votes": ce_v, "pe_votes": pe_v, "votes": votes,
        "data_as_of": v["data_date"], "fetched_at": now_str,
        "data_warning": f"Signals based on {v['data_date']} close (yfinance EOD). Strike interval is an approximation — verify actual listed strikes in Kite option chain. No lot size shown (not available from any live source) — figures are per-share, not total cost.",
        "trade": trade,
    }


def main():
    now_str = now_ist_str()
    results = {"_meta": {"generated_at": now_str, "method": "Same 3-factor consensus as refresh_fo_cloud.py (EMA alignment, MACD, pivot breakout, ROC5). votes==6 -> single-leg, votes in [4,5] -> capped-risk spread."}}
    errors = {}
    for name in TRACKED_STOCKS:
        try:
            r = _stock_signal(name)
            if "error" in r:
                errors[name] = r["error"]
            else:
                results[name] = r
                print(f"  {name}: {r['consensus']} | spot={r['spot']}" + (f" | {r['trade']['type']}" if r.get('trade') else ""))
        except Exception as e:
            errors[name] = str(e)
            print(f"  {name} ERROR: {e}")

    results["_meta"]["errors"] = errors
    OUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
