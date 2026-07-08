"""
Stock F&O Signals — live strike/premium/expiry for individual stock options.

Replaces a static HTML table (frozen at a 25-Jun snapshot) that showed a
"Score" column from a methodology not present anywhere in this codebase
(likely hand-generated externally, same situation as the dashboard's
SCAN_DATA). Rather than guess at reproducing an unknown scoring system,
this reuses the SAME transparent 3-factor consensus already proven for
NIFTY/BANKNIFTY in refresh_fo_cloud.py — documented, not invented.

Strategy selection -- CHANGED 08-Jul-2026, was a documented rule that turned
out to be backwards, not a guess:
  - Previously: ce_votes/pe_votes == 6 (max conviction, all 4 factors agree)
    routed to single-leg BUY CE/PE ("full premium exposure for the highest-
    confidence setups"); 4-5 votes routed to a capped-risk spread.
  - backtest_fo_results.json (3y, run 08-Jul-2026) showed this was backwards:
    the 6-vote single-leg bucket has the WORST risk-adjusted numbers of any
    vote bucket (846 trades, 39.7% win, Sharpe 2.71, avg return 10.95%) --
    worse than BOTH spread buckets it was supposedly a step up from (4-vote:
    601 trades, 41.9% win, Sharpe 4.83, avg 92.84%; 5-vote: 170 trades, 45.3%
    win, Sharpe 3.04, avg 110.85%). "Reward higher conviction with full
    exposure" was the design intent; the data says full exposure is where
    this system's edge is weakest, not strongest.
  - Now: ALL actionable signals (any vote count that reaches consensus) route
    to the capped-risk spread. Single-leg generation is disabled, not
    deleted -- _single_leg() is still defined below in case a proper
    vote-count-vs-structure backtest (the current data confounds the two:
    single-leg was only ever tested AT 6 votes, never at 4-5, so we don't
    have a clean "6-vote spread" comparison either) later justifies bringing
    it back for a specific bucket. Until then, defaulting to the ONLY
    structure with ANY evidenced support, at every vote level, is the
    capital-protective choice under incomplete evidence -- not a coin flip.

KNOWN LIMITATION — strike intervals: NSE sets per-stock strike intervals
that vary by price band and aren't available from any source in this repo.
_strike_step() below is a documented heuristic approximation, not official
exchange data. Every output is labeled accordingly — verify the actual
listed strikes in your broker's option chain before trading.

KNOWN LIMITATION — no lot size: NSE F&O lot sizes are revised periodically
and aren't available from any live source here, so cost-per-lot is
intentionally omitted (not guessed). All premium/SL/target figures are in
₹-per-share and %, not total position cost.

MACRO WIRING (added 08-Jul-2026): every actionable signal is annotated with
macro_context (risk_level/bias/risk_score/position_size_multiplier at
generation time) and macro_blocked (true when macro_risk.json's
trade_adjustments blocks that direction — see macro_gate.py). Unlike
trade_brain.py/expert_gate.py, a macro block does NOT suppress the trade
suggestion here — there's no automated open on this path, a human decides
manually, and hiding the raw technical signal would contradict the macro
overlay's own "does not replace the technical signal" disclaimer. It's
flagged loudly in data_warning instead.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date

from ist_time import now_ist_str
from refresh_fo_cloud import _get_indicators, _signals, _signal_commentary, _premium_estimate, _next_monthly_expiry, EXPIRY_ENTRY_CAUTION_DAYS
from macro_gate import load_macro_risk, direction_blocked, macro_context

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
    prem = _premium_estimate(spot, strike, ann_vol, days, opt)
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
    long_prem = _premium_estimate(spot, atm_strike, ann_vol, days, opt)
    short_prem = _premium_estimate(spot, otm_strike, ann_vol, days, opt)
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


def _stock_signal(name, macro):
    v = _get_indicators(name + ".NS")
    if not v:
        return {"error": f"No data for {name}"}
    consensus, ce_v, pe_v = _signals(v)
    commentary = _signal_commentary(v, ce_v, pe_v, consensus)
    spot = v["spot"]
    now_str = now_ist_str()

    # FIXED 07-Jul-2026: this used to hardcode "yfinance EOD" unconditionally,
    # same bug already fixed the same day in refresh_fo_cloud.py/equity_scan_core.py/
    # sector_rotation_core.py (found and left unfixed here during the audit
    # compilation pass, now closed). v["data_source"] ("kite"/"yfinance") was
    # already being computed by _get_indicators() this whole time -- just never
    # surfaced in the output.
    warning_prefix = (
        f"Signals based on {v['data_date']} Kite historical data (official, live session) — still end-of-day candles, not tick-level."
        if v["data_source"] == "kite" else
        f"Signals based on {v['data_date']} close (yfinance EOD, delayed/unofficial)."
    )

    if consensus == "WAIT":
        return {
            "name": name, "spot": spot, "consensus": "WAIT",
            "ce_votes": ce_v, "pe_votes": pe_v,
            "ann_vol": v["ann_vol"],
            "data_as_of": v["data_date"], "fetched_at": now_str,
            "data_source": v["data_source"],
            "data_warning": f"{warning_prefix} Verify in Kite before trading.",
            "trade": None,
            "commentary": commentary,
            "macro_context": macro_context(macro),
        }

    opt = "CE" if consensus == "BUY_CE" else "PE"
    votes = ce_v if consensus == "BUY_CE" else pe_v
    expiry = _next_monthly_expiry(MONTHLY_EXPIRY_WEEKDAY)
    days_to = max((expiry - date.today()).days, 1)
    step = _strike_step(spot)
    atm = _atm(spot, step)

    # Single-leg routing at votes>=6 disabled 08-Jul-2026 -- see module
    # docstring. Every actionable signal now gets the capped-risk spread,
    # the only structure with backtested support at any vote level tested.
    otm = atm + 2 * step if opt == "CE" else atm - 2 * step
    trade = _spread(spot, atm, otm, opt, v["ann_vol"], days_to)

    trade["expiry"] = expiry.strftime("%d %b %Y")
    trade["days_to_exp"] = days_to
    trade["strike_step_used"] = step

    macro_blocked, macro_reason = direction_blocked(macro, consensus)
    near_expiry = days_to <= EXPIRY_ENTRY_CAUTION_DAYS
    trade["near_expiry_caution"] = near_expiry

    # No real option-chain data source exists anywhere in this repo (no OI,
    # no bid-ask) -- the strike itself is chosen purely by moneyness
    # (_atm()) against an already-approximate strike-interval guess
    # (_strike_step()). Unlike NIFTY/BANKNIFTY (where ATM is reliably the
    # deepest liquidity on the exchange), single-stock ATM strikes can have
    # real OI/spread problems this system has no way to detect. Said plainly
    # every time, not just in the module docstring.
    warning = (f"{warning_prefix} Strike interval is an approximation — verify actual listed strikes in Kite option chain. "
               f"No lot size shown (not available from any live source) — figures are per-share, not total cost. "
               f"⚠ LIQUIDITY UNVERIFIED — no open-interest or bid-ask data exists anywhere in this system; "
               f"this strike is chosen by moneyness only and may be thin or wide-spread. Check the live option chain before assuming it's fillable near this premium.")
    if macro_blocked:
        warning = f"⚠ {macro_reason}. This is still the raw technical signal (not suppressed — a human decides manually here), but treat it as a NO-GO until macro risk eases. {warning}"
    if near_expiry:
        warning = f"⚠ EXPIRY-DAY CAUTION — expires in {days_to}d, extreme gamma/theta risk for a NEW position. {warning}"
    if votes >= 6:
        # NOT an active warning anymore -- informational context for WHY
        # every vote level gets the same spread structure now (see module
        # docstring: single-leg routing at votes>=6 was disabled 08-Jul-2026
        # because backtest_fo_results.json showed it was this system's
        # worst-performing bucket). The routing fix already addresses the
        # risk this used to warn about; this note explains the history so
        # "6/6" still isn't misread as extra conviction deserving anything
        # different from a 4/6 or 5/6 signal.
        warning = (f"ℹ 6/6 still gets the same capped-risk spread as every other vote level, not extra exposure — "
                   f"this system's own 3-year backtest found the old \"6-vote = single-leg\" rule was actually its "
                   f"worst-performing bucket (39.7% win, Sharpe 2.71 vs 41.9-45.3% win, Sharpe 3.0-4.8 for spreads). "
                   f"Don't read '6/6' as higher conviction than a 4/6 or 5/6 signal. {warning}")

    return {
        "name": name, "spot": spot, "consensus": consensus,
        "ce_votes": ce_v, "pe_votes": pe_v, "votes": votes,
        "ann_vol": v["ann_vol"],  # surfaced so recommendation_tracker.py can re-price single-leg stock F&O recs (same as the index engine already exposes)
        "data_as_of": v["data_date"], "fetched_at": now_str,
        "data_source": v["data_source"],
        "data_warning": warning,
        "trade": trade,
        "commentary": commentary,
        "macro_context": macro_context(macro),
        "macro_blocked": macro_blocked,
    }


def main():
    now_str = now_ist_str()
    macro = load_macro_risk()
    results = {"_meta": {"generated_at": now_str, "method": "Same 3-factor consensus as refresh_fo_cloud.py (EMA alignment, MACD, pivot breakout, ROC5). votes==6 -> single-leg, votes in [4,5] -> capped-risk spread.",
                          "macro_feed_available": macro is not None}}
    errors = {}
    for name in TRACKED_STOCKS:
        try:
            r = _stock_signal(name, macro)
            if "error" in r:
                errors[name] = r["error"]
            else:
                results[name] = r
                print(f"  {name}: {r['consensus']} | spot={r['spot']}" + (f" | {r['trade']['type']}" if r.get('trade') else "")
                      + (" [MACRO BLOCKED]" if r.get('macro_blocked') else ""))
        except Exception as e:
            errors[name] = str(e)
            print(f"  {name} ERROR: {e}")

    # Real aggregate across whatever each stock actually used this run --
    # same pattern as fo_latest.json/equity_scan.json/sector_rotation.json's
    # _meta.source (fixed the same day, this file was the one instance missed).
    sources = [r["data_source"] for r in results.values() if isinstance(r, dict) and r.get("data_source")]
    kite_n, yf_n = sources.count("kite"), sources.count("yfinance")
    if sources and yf_n == 0:
        source_label = "kite (official, live session)"
    elif sources and kite_n == 0:
        source_label = "yfinance EOD (delayed/unofficial)"
    elif sources:
        source_label = f"mixed: {kite_n} kite (live) / {yf_n} yfinance EOD (delayed)"
    else:
        source_label = "unknown (no stock returned data_source)"
    results["_meta"]["source"] = source_label
    results["_meta"]["errors"] = errors
    OUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
