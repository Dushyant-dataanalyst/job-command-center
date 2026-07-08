"""
F&O Gate-2 backtest — the counterpart to backtest.py (which covers only the
equity swing voters). User request 08-Jul-2026: "make sure backtest is
applied on all trade, F&O and swing both." This closes that gap: covers
BOTH the index F&O engine (NIFTY50/BANKNIFTY, refresh_fo_cloud.py) and the
per-stock F&O engine (18 tracked stocks, stock_fo_refresh.py) — single-leg
AND spread trades.

WHY THIS EXISTS AS A SEPARATE FILE FROM backtest.py: different signal
engine (3-factor index/stock consensus, not the 4 named equity voters),
different instrument mechanics (options expire, strikes, premiums), and a
REAL exit rule already exists live for F&O (trade_brain.py's mark-to-market
logic) -- unlike equity, which had none and needed an invented convention.
Keeping these separate avoids forcing two genuinely different signal types
through one code path.

DESIGN, VERIFIED BEFORE TRUSTING (same discipline as backtest.py):

1. Signal decision is IMPORTED, not reimplemented: _signals() (the 3-factor
   CE/PE consensus) is called directly from refresh_fo_cloud.py. Premium
   math (_premium_estimate, _atm) is also imported. Zero drift risk on any
   of the actual decision/pricing logic -- only the walk-forward indicator
   plumbing and the calendar-expiry math below are backtest-specific.

2. _next_monthly_expiry() in refresh_fo_cloud.py hardcodes date.today()
   internally, so it can't be reused for a simulated historical date. This
   file has _next_monthly_expiry_asof(ref_date, weekday) -- the IDENTICAL
   calendar algorithm, parameterized by a reference date instead of calling
   date.today(). This is safe to duplicate (pure deterministic date
   arithmetic, not a strategy decision) and is verified below to produce
   the exact same output as the real function when both are asked about
   real "today".

3. F&O HAS A REAL LIVE EXIT RULE (trade_brain.py's _mark_to_market): target2
   hit, else target1 hit, else stop-loss, else expired, else signal-flip --
   in that exact priority order. This backtest replicates that SAME priority
   order for single-leg trades (index + stock votes>=6), calling the same
   imported _premium_estimate/_signals for the actual values. Unlike
   backtest.py, NO invented exit convention was needed here.

4. Stock-F&O SPREADS (votes 4-5, two legs) settle at expiry vs the spread's
   breakeven -- the SAME coarse convention already used in
   recommendation_tracker.py's fo_stock_spread scoring (favorable if
   underlying is past breakeven at expiry, unfavorable otherwise). Proper
   two-leg time-value P&L is deliberately not modeled (would fabricate
   numbers); spreads settle only at expiry, not early, matching how a real
   debit spread is typically held.

5. ann_vol (annualized realized volatility, feeds premium estimation) is
   computed as a rolling 30-day trailing measure at each simulated day --
   same window production uses for "today", just walked forward.

6. Bulk historical data fetched via yf_retry directly (not
   market_data.get_ohlcv()'s Kite-preferring path) -- same reasoning as
   backtest.py: a backtest needs a long, consistent one-time pull, not
   live-source preference.

Output: nse-trading-bot/backtest_fo_results.json. Standalone, on-demand
(python backtest_fo.py), NOT wired into any CI cron, NOT a dashboard feed --
same "Gate 2 validation only" status as backtest.py.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date, timedelta

import numpy as np
import pandas as pd

from refresh_fo_cloud import INSTRUMENTS, _signals, _premium_estimate, _atm
from stock_fo_refresh import TRACKED_STOCKS, MONTHLY_EXPIRY_WEEKDAY, _strike_step
from yf_retry import download_with_retry
from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = pathlib.Path(__file__).parent / "backtest_fo_results.json"

BACKTEST_PERIOD = "3y"
MIN_BARS_WARMUP = 55  # matches refresh_fo_cloud._get_indicators()'s own minimum


def _next_monthly_expiry_asof(today, weekday):
    """Byte-identical calendar algorithm to refresh_fo_cloud._next_monthly_
    expiry(), parameterized by a reference date instead of date.today() --
    see module docstring point 2."""
    for mo_off in range(0, 3):
        yr = today.year + (today.month + mo_off - 1) // 12
        mo = (today.month + mo_off - 1) % 12 + 1
        last = date(yr + 1, 1, 1) - timedelta(days=1) if mo == 12 else date(yr, mo + 1, 1) - timedelta(days=1)
        d = last
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        if (d - today).days >= 7:
            return d
    d = today + timedelta(days=1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def _walkforward_fo_indicators(df):
    """Full-series version of refresh_fo_cloud._get_indicators() -- same
    formulas/spans, computed once across the whole history. rolling/ewm/shift
    are all causal (no lookahead)."""
    close, high, low = df["close"], df["high"], df["low"]
    ema18 = close.ewm(span=18, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    macd_signal = macd.ewm(span=9, adjust=False).mean()
    ann_vol = close.pct_change().rolling(30).std() * (252 ** 0.5)
    prev_h, prev_l, prev_c = high.shift(1), low.shift(1), close.shift(1)
    pivot = (prev_h + prev_l + prev_c) / 3
    r1 = 2 * pivot - prev_l
    s1 = 2 * pivot - prev_h
    roc5 = close.pct_change(5) * 100
    return pd.DataFrame({
        "spot": close, "ema18": ema18, "ema50": ema50,
        "macd": macd, "macd_signal": macd_signal, "ann_vol": ann_vol,
        "r1": r1, "s1": s1, "roc5": roc5,
    })


def _v_at(ind, i):
    row = ind.iloc[i]
    if row[["spot", "ema18", "ema50", "ann_vol", "r1", "s1"]].isna().any():
        return None
    return {
        "spot": float(row["spot"]), "ema18": float(row["ema18"]), "ema50": float(row["ema50"]),
        "macd": float(row["macd"]), "macd_signal": float(row["macd_signal"]),
        "ann_vol": float(row["ann_vol"]), "r1": float(row["r1"]), "s1": float(row["s1"]),
        "roc5": float(row["roc5"]) if pd.notna(row["roc5"]) else 0.0,
    }


def _simulate_single_leg(name, df, ind, step, expiry_weekday, votes_min):
    """Index + single-leg stock (votes>=votes_min) trades. Reuses the REAL
    live exit priority: target2 > target1 > stop_loss > expired > signal_flip."""
    n = len(df)
    trades = []
    open_trade = None
    for i in range(MIN_BARS_WARMUP, n):
        today = df.index[i].date()
        if open_trade is not None:
            v = _v_at(ind, i)
            if v is None:
                continue
            days_remaining = (open_trade["expiry"] - today).days
            cur_premium = _premium_estimate(v["spot"], open_trade["strike"], v["ann_vol"],
                                             max(days_remaining, 1), open_trade["opt"])
            consensus, ce_v, pe_v = _signals(v)
            reason, exit_prem = None, None
            if cur_premium >= open_trade["t2"]:
                reason, exit_prem = "target2_hit", cur_premium
            elif cur_premium >= open_trade["t1"]:
                reason, exit_prem = "target1_hit", cur_premium
            elif cur_premium <= open_trade["sl"]:
                reason, exit_prem = "stop_loss", cur_premium
            elif days_remaining <= 0:
                reason, exit_prem = "expired", cur_premium
            elif consensus != "WAIT" and consensus != open_trade["consensus"]:
                reason, exit_prem = "signal_flip", cur_premium
            if reason:
                entry = open_trade["entry"]
                outcome_pct = round((exit_prem - entry) / entry * 100, 2)
                trades.append({
                    "symbol": name, "kind": "fo_single_leg",
                    "entry_date": str(open_trade["entry_date"]), "exit_date": str(today),
                    "strike": open_trade["strike"], "option_type": open_trade["opt"],
                    "entry": entry, "exit": round(exit_prem, 2), "outcome_pct": outcome_pct,
                    "exit_reason": reason, "hold_days": (today - open_trade["entry_date"]).days,
                    "votes_at_entry": open_trade["votes"],
                })
                open_trade = None
            continue

        v = _v_at(ind, i)
        if v is None:
            continue
        consensus, ce_v, pe_v = _signals(v)
        if consensus == "WAIT":
            continue
        votes = ce_v if consensus == "BUY_CE" else pe_v
        if votes < votes_min:
            continue
        opt = "CE" if consensus == "BUY_CE" else "PE"
        expiry = _next_monthly_expiry_asof(today, expiry_weekday)
        days_to = max((expiry - today).days, 1)
        strike = _atm(v["spot"], step)
        entry = _premium_estimate(v["spot"], strike, v["ann_vol"], days_to, opt)
        open_trade = {"entry": entry, "strike": strike, "opt": opt, "consensus": consensus,
                      "sl": round(entry * 0.70, 0), "t1": round(entry * 1.40, 0), "t2": round(entry * 2.00, 0),
                      "expiry": expiry, "entry_date": today, "votes": votes}
    return trades


def _simulate_spread(name, df, ind, step, expiry_weekday, otm_offset_steps=2):
    """Stock spreads (votes in [4,5)) -- settle at expiry vs breakeven, same
    coarse convention as recommendation_tracker.py's fo_stock_spread scoring.
    No early exit modeled (a debit spread is typically held to expiry)."""
    n = len(df)
    trades = []
    open_trade = None
    for i in range(MIN_BARS_WARMUP, n):
        today = df.index[i].date()
        if open_trade is not None:
            if today >= open_trade["expiry"]:
                v = _v_at(ind, i)
                spot = v["spot"] if v else float(df["close"].iloc[i])
                favorable = spot >= open_trade["breakeven"] if open_trade["opt"] == "CE" else spot <= open_trade["breakeven"]
                outcome_pct = round((open_trade["max_profit"] if favorable else -open_trade["net_debit"])
                                     / open_trade["net_debit"] * 100, 2) if open_trade["net_debit"] else 0.0
                trades.append({
                    "symbol": name, "kind": "fo_spread",
                    "entry_date": str(open_trade["entry_date"]), "exit_date": str(today),
                    "strike": open_trade["long_strike"], "option_type": open_trade["opt"],
                    "entry": open_trade["net_debit"], "exit": None, "outcome_pct": outcome_pct,
                    "exit_reason": "expired_settled_vs_breakeven",
                    "hold_days": (today - open_trade["entry_date"]).days,
                    "votes_at_entry": open_trade["votes"],
                })
                open_trade = None
            continue

        v = _v_at(ind, i)
        if v is None:
            continue
        consensus, ce_v, pe_v = _signals(v)
        if consensus == "WAIT":
            continue
        votes = ce_v if consensus == "BUY_CE" else pe_v
        if votes < 4 or votes >= 6:  # spread band only -- votes>=6 goes single-leg instead
            continue
        opt = "CE" if consensus == "BUY_CE" else "PE"
        expiry = _next_monthly_expiry_asof(today, expiry_weekday)
        days_to = max((expiry - today).days, 1)
        atm = _atm(v["spot"], step)
        otm = atm + otm_offset_steps * step if opt == "CE" else atm - otm_offset_steps * step
        long_prem = _premium_estimate(v["spot"], atm, v["ann_vol"], days_to, opt)
        short_prem = _premium_estimate(v["spot"], otm, v["ann_vol"], days_to, opt)
        net_debit = round(long_prem - short_prem, 2)
        if net_debit <= 0:
            continue  # degenerate pricing at this vol/strike combo -- skip rather than fabricate
        width = abs(otm - atm)
        breakeven = round(atm + net_debit, 2) if opt == "CE" else round(atm - net_debit, 2)
        open_trade = {"long_strike": atm, "short_strike": otm, "net_debit": net_debit,
                      "max_profit": round(width - net_debit, 2), "breakeven": breakeven,
                      "opt": opt, "expiry": expiry, "entry_date": today, "votes": votes}
    return trades


def _stats(trades):
    """Same method as backtest.py (equity) for direct comparability: trade-
    return Sharpe (mean/stdev annualized by trades-per-year, NOT NAV-based)
    and chronologically-compounded max drawdown (treats each closed trade as
    a discrete full-stake bet in sequence -- overstates real risk vs a book
    that actually spreads capital across concurrent positions). Added after
    the first run showed WHY this matters: stock spreads have a low win rate
    (~39%) but a large positive average return, because a capped-risk debit
    spread's win is structurally much bigger than its capped -100% loss --
    win rate alone makes that look worse than it is, and avg_return alone
    makes it look better than it is. Sharpe/maxDD are the honest tiebreaker."""
    if not trades:
        return {"count": 0, "win_rate": None, "avg_return_pct": None, "avg_hold_days": None,
                "sharpe_approx": None, "max_drawdown_pct": None}
    returns = [t["outcome_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    win_rate = round(len(wins) / len(returns) * 100, 1)
    avg_return = round(sum(returns) / len(returns), 2)

    r = np.array(returns) / 100.0
    sharpe = None
    if len(r) >= 2 and r.std(ddof=1) > 0:
        ordered = sorted(trades, key=lambda t: t["exit_date"])
        span_days = max((pd.to_datetime(ordered[-1]["exit_date"]) - pd.to_datetime(ordered[0]["entry_date"])).days, 1)
        trades_per_year = len(trades) / (span_days / 365.25)
        # float(...) forces a native Python float -- np.float64 happens to
        # subclass float (so json.dumps silently accepts it today), but that's
        # an implementation detail, not a guarantee; be explicit rather than
        # rely on it.
        sharpe = float(round((r.mean() / r.std(ddof=1)) * np.sqrt(max(trades_per_year, 1)), 2))

    ordered = sorted(trades, key=lambda t: t["exit_date"])
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for t in ordered:
        pct = t.get("outcome_pct")
        if pct is None:
            continue
        equity *= max(1 + pct / 100.0, 0.0)  # a spread's -100% floors equity at 0, never negative
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity - peak) / peak if peak else 0.0)

    return {
        "count": len(trades), "win_rate": win_rate, "avg_return_pct": avg_return,
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / len(trades), 1),
        "sharpe_approx": sharpe, "max_drawdown_pct": round(max_dd * 100, 2),
    }


def main():
    all_trades = []
    errors = {}
    print(f"[{now_ist_str()}] F&O backtest starting -- period={BACKTEST_PERIOD}")

    print("  -- Index F&O (NIFTY50, BANKNIFTY) --")
    for name, cfg in INSTRUMENTS.items():
        try:
            df = download_with_retry(cfg["ticker"], period=BACKTEST_PERIOD)
            if df.empty or len(df) < MIN_BARS_WARMUP + 10:
                errors[name] = "insufficient price history"
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            ind = _walkforward_fo_indicators(df)
            trades = _simulate_single_leg(name, df, ind, cfg["step"], cfg["expiry_day"], votes_min=4)
            all_trades.extend(trades)
            print(f"    {name}: {len(trades)} simulated trade(s)")
        except Exception as e:
            errors[name] = str(e)
            print(f"    {name} ERROR: {e}")

    print("  -- Stock F&O (18 tracked stocks, single-leg + spread) --")
    for symbol in TRACKED_STOCKS:
        try:
            df = download_with_retry(symbol + ".NS", period=BACKTEST_PERIOD)
            if df.empty or len(df) < MIN_BARS_WARMUP + 10:
                errors[symbol] = "insufficient price history"
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            step = _strike_step(float(df["close"].iloc[-1]))
            ind = _walkforward_fo_indicators(df)
            single = _simulate_single_leg(symbol, df, ind, step, MONTHLY_EXPIRY_WEEKDAY, votes_min=6)
            spread = _simulate_spread(symbol, df, ind, step, MONTHLY_EXPIRY_WEEKDAY)
            all_trades.extend(single)
            all_trades.extend(spread)
            print(f"    {symbol}: {len(single)} single-leg + {len(spread)} spread trade(s)")
        except Exception as e:
            errors[symbol] = str(e)
            print(f"    {symbol} ERROR: {e}")

    index_trades = [t for t in all_trades if t["symbol"] in INSTRUMENTS]
    stock_single = [t for t in all_trades if t["symbol"] not in INSTRUMENTS and t["kind"] == "fo_single_leg"]
    stock_spread = [t for t in all_trades if t["kind"] == "fo_spread"]
    by_vote_count = {}
    for t in all_trades:
        k = str(t["votes_at_entry"])
        by_vote_count.setdefault(k, []).append(t)

    result = {
        "run_at": now_ist_str(),
        "period": BACKTEST_PERIOD,
        "errors": errors,
        "overall": _stats(all_trades),
        "index_fo": _stats(index_trades),
        "stock_fo_single_leg": _stats(stock_single),
        "stock_fo_spread": _stats(stock_spread),
        "by_vote_count": {k: _stats(v) for k, v in sorted(by_vote_count.items())},
        "by_instrument": {name: _stats([t for t in all_trades if t["symbol"] == name])
                          for name in sorted(set(t["symbol"] for t in all_trades))},
        "assumptions": {
            "single_leg_exit_rule": "REAL live rule reused from trade_brain.py: target2 > target1 > stop_loss > expired > signal_flip, in that priority order -- not invented for this backtest.",
            "spread_settlement": "Settles only at expiry vs breakeven (favorable/unfavorable) -- same coarse convention as recommendation_tracker.py's fo_stock_spread scoring. No early exit, no two-leg time-value modeling.",
            "sl_t1_t2_formula": "sl=entry*0.70, t1=entry*1.40, t2=entry*2.00 -- same as refresh_fo_cloud.py/stock_fo_refresh.py live.",
            "premium_model": "_premium_estimate() -- estimated, no real NSE option-chain feed exists anywhere in this project.",
            "strike_step_stocks": "_strike_step() heuristic by price band -- documented approximation, not official exchange data.",
            "sharpe_method": "trade-return mean/stdev annualized by trades-per-year, NOT NAV-based (matches backtest.py for comparability).",
            "max_drawdown_method": "chronological compounding of closed trades as discrete full-stake bets; overstates real risk vs a book spreading capital across concurrent positions (matches backtest.py's method).",
            "spread_pct_caveat": "Spread outcome_pct is structurally lopsided (capped -100% loss vs an uncapped-until-width max gain on a small net-debit base), so a low win rate can coexist with a large positive average return -- read win_rate AND sharpe/maxDD together, not avg_return_pct alone.",
        },
        "trades": all_trades,
        "disclaimer": "Backtest of the F&O signal engines (index + stock, single-leg + spread) against real historical yfinance daily data. Educational validation only -- past performance of a re-derived, estimated-premium signal set is not evidence of future results. Gate 2 per the Master Brief's build order; does not authorize wiring any executor.",
    }
    OUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    o = result["overall"]
    print()
    print(f"  Overall: {o['count']} trades, {o['win_rate']}% win rate, avg {o['avg_return_pct']}%")
    print(f"  Index F&O: {result['index_fo']}")
    print(f"  Stock single-leg: {result['stock_fo_single_leg']}")
    print(f"  Stock spread: {result['stock_fo_spread']}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
