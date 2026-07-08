"""
Gate 2 backtest — Master Brief's own stated build order is "audit -> approval
-> backtest FIRST -> executor -> filters -> safety." This is that module.
Read-only, no order code, no live-trading risk: it replays the 4 existing
equity voters (Inna/Pham/Cianni/Unger, equity_scan_core.py) against real
historical price data and reports whether they would actually have worked.

WHY THIS EXISTS: the live signal engine (equity_scan_core.py) is a
reconstruction from one-line strategy-name hints, never verified against an
original spec (see that file's own docstring). This is the only way to get
an empirical read on whether it has real edge, since the live paper-trading
journal (trade_journal.json) only has 2 closed trades so far -- nowhere near
enough to draw a conclusion.

DESIGN CHOICES MADE HERE (none of these are extracted facts -- flagged
explicitly so nobody mistakes them for verified behavior):

1. Voting logic is IMPORTED, not reimplemented. _strategy_inna/_pham/_cianni/
   _unger are called directly from equity_scan_core.py -- the decision tested
   here is byte-identical to what's live. Only the indicator-snapshot-for-
   day-T plumbing below is backtest-specific (production only ever needs the
   latest bar; a walk-forward needs every historical bar's snapshot).

2. PEG ratio is NOT backtested -- yfinance has no historical PEG endpoint,
   so Pham's fundamental gate is fed peg=None throughout (matching
   production's own "PEG unavailable doesn't block" behavior). This only
   validates Pham's technical half (EMA stack + RSI recovery).

3. Voter weights are FLAT EQUAL (0.25 each) for the entire backtest window.
   Replaying voter_weights_refresh.py's learning process retroactively
   (recomputing what weights WOULD have been at each point in history) is a
   materially bigger undertaking and out of scope for a first Gate-2 pass.
   This also matches the system's actual real-world state today -- there
   isn't yet enough real closed-trade history for weights to have diverged
   from equal.

4. NO EXIT RULE EXISTS in the live system for equity signals -- equity_
   scan_core.py just re-scores daily, equity_brain.py only labels current
   status (healthy/below_sl/target_hit), neither auto-closes anything. This
   backtest therefore INVENTS one, the same spirit as trade_brain.py's F&O
   exit logic: close at whichever of (SL hit, T1 hit, MAX_HOLDING_DAYS
   elapsed) comes first, walking forward day-by-day from entry. SL/T1/T2/T3
   themselves reuse the exact formula already live in equity_scan_core.py's
   scan_one() (SL = entry - 1.5*ATR, T1 = entry + 1.25*risk, etc.).

5. rel_volume here approximates production's "last nonzero-volume day vs
   mean of the previous 20 nonzero-volume days" as a simpler trailing 20-day
   mean with zero-volume days NaN'd out (not the exact same nonzero-day
   reindexing production does). Disclosed as a known minor approximation,
   same honesty standard as this project's other documented simplifications
   (see RSI note below).

6. RSI here deliberately replicates the SAME simple-rolling-mean formula
   equity_scan_core.py actually uses live (not the textbook Wilder-smoothed
   average) -- testing what's REALLY live matters more than testing a
   corrected version nobody is running.

7. Sharpe is trade-return-based: mean/stdev of each closed trade's % return,
   annualized by (trades per year), NOT a NAV/equity-curve Sharpe. A common
   retail-backtest approximation, not a precise institutional figure.
   Max drawdown compounds closed trades in chronological sequence as if
   each were a discrete full-capital bet -- it does not account for running
   multiple concurrent stock positions with split capital. Both simplifications
   are standard for this kind of single-strategy backtest, but overstate risk
   concentration versus how a real multi-position book would actually behave.

8. Bulk historical data is fetched via yfinance directly (yf_retry), not
   market_data.get_ohlcv()'s Kite-preferring path -- a backtest doesn't need
   live-data preference, it needs a long (3y) consistent pull. Kite's own
   period-to-lookback map (market_data._PERIOD_DAYS) used to have no "3y"
   entry and would silently truncate to its 190-day default instead of
   raising or falling back -- fixed (added the "3y" entry, and an unmapped
   period now returns None to force the existing yfinance-fallback path
   rather than guessing), so this bypass is now belt-and-suspenders rather
   than a workaround for a live bug.

Output: nse-trading-bot/backtest_results.json (NOT wired into any CI cron,
NOT a dashboard feed, NOT registered in vercel.json/validate_json_outputs.py
-- deliberately a standalone, on-demand validation artifact, matching this
prompt's own "Gate 2 validation only" framing. Run manually:
    cd nse-trading-bot && python backtest.py
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

from equity_scan_core import (
    _adx14, _ticker_sector_map,
    _strategy_inna, _strategy_pham, _strategy_cianni, _strategy_unger,
)
from yf_retry import download_with_retry
from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = pathlib.Path(__file__).parent / "backtest_results.json"

BACKTEST_PERIOD = "3y"
MIN_BARS_WARMUP = 55         # matches equity_scan_core.py's own minimum-history gate
MAX_HOLDING_DAYS = 20         # invented convention -- see module docstring point 4
VOTERS = ["Inna", "Pham", "Cianni", "Unger"]
EQUAL_WEIGHT = 0.25           # see module docstring point 3


def _walkforward_indicators(df):
    """Full-series version of equity_scan_core._extended_indicators() --
    same formulas, same spans/windows, computed once across the whole
    history instead of just the last bar, so every row is a valid
    'as of that day' snapshot with no lookahead (rolling/ewm/shift are all
    inherently causal)."""
    close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema18 = close.ewm(span=18, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    tr = pd.concat([(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean().replace(0, np.nan)
    rsi = 100 - (100 / (1 + gain / loss))
    rsi_3d_ago = rsi.shift(3)
    adx = _adx14(high, low, close)
    high20_prev = high.rolling(20).max().shift(1)
    high50_prev = high.rolling(50).max().shift(1)
    low10_prev = low.rolling(10).min().shift(1)
    low_recent_min = low.rolling(3).min()
    prev_close = close.shift(1)
    # Approximation of production's nonzero-volume-day ratio -- see docstring
    # point 5. Numerator is forward-filled past any zero-volume day (matching
    # production's "skip zero days, use the last real one" intent for
    # *today's* volume specifically) -- without this, any row whose OWN
    # volume happens to read 0 (e.g. a still-forming latest bar) would divide
    # NaN/x and silently return None instead of a real number, which is a
    # worse failure mode than the documented approximation and was caught by
    # cross-checking this function's last row against production's actual
    # output for the same ticker (they now match exactly).
    vol_nan_zero = vol.replace(0, np.nan)
    vol_numerator = vol_nan_zero.ffill()
    # min_periods=10: pandas' rolling().mean() default requires ALL 20 window
    # values to be non-NaN, so a single genuine zero-volume day anywhere in
    # the trailing window poisons the next 19 rows' averages to NaN too, not
    # just that one day. Discovered by the cross-check below returning NaN
    # for several real, liquid, large-cap stocks where that should never
    # happen. 10-of-20 is a reasonable floor, not a verified-optimal choice.
    rel_volume = vol_numerator / vol_nan_zero.rolling(20, min_periods=10).mean().shift(1)

    return pd.DataFrame({
        "spot": close, "ema9": ema9, "ema18": ema18, "ema20": ema20, "ema50": ema50,
        "atr": atr, "rsi": rsi, "rsi_3d_ago": rsi_3d_ago, "adx": adx,
        "high20_prev": high20_prev, "high50_prev": high50_prev, "low10_prev": low10_prev,
        "low_recent_min": low_recent_min, "prev_close": prev_close, "rel_volume": rel_volume,
    })


def _v_at(ind, i):
    row = ind.iloc[i]
    if row[["spot", "ema18", "ema50", "atr"]].isna().any():
        return None  # not enough warmup yet at this row
    return {
        "spot": float(row["spot"]), "ema9": float(row["ema9"]), "ema18": float(row["ema18"]),
        "ema20": float(row["ema20"]), "ema50": float(row["ema50"]), "atr": float(row["atr"]),
        "rsi": float(row["rsi"]) if pd.notna(row["rsi"]) else None,
        "rsi_3d_ago": float(row["rsi_3d_ago"]) if pd.notna(row["rsi_3d_ago"]) else None,
        "adx": float(row["adx"]) if pd.notna(row["adx"]) else None,
        "high20_prev": float(row["high20_prev"]) if pd.notna(row["high20_prev"]) else None,
        "high50_prev": float(row["high50_prev"]) if pd.notna(row["high50_prev"]) else None,
        "low10_prev": float(row["low10_prev"]) if pd.notna(row["low10_prev"]) else None,
        "low_recent_min": float(row["low_recent_min"]),
        "prev_close": float(row["prev_close"]) if pd.notna(row["prev_close"]) else None,
        "rel_volume": float(row["rel_volume"]) if pd.notna(row["rel_volume"]) else None,
    }


def _weighted_signal(strategies):
    """Same math as equity_scan_core.scan_one(), flat equal weights (point 3)."""
    weighted_votes = sum(EQUAL_WEIGHT * 4 for sig in strategies.values() if sig in ("BUY", "STRONG_BUY"))
    if weighted_votes >= 3:
        return "STRONG_BUY"
    if weighted_votes >= 2:
        return "BUY"
    return "WATCH"


def _simulate_stock(symbol, sector, df):
    ind = _walkforward_indicators(df)
    n = len(df)
    trades = []
    open_trade = None

    for i in range(MIN_BARS_WARMUP, n):
        if open_trade is not None:
            days_held = i - open_trade["entry_idx"]
            day_low, day_high, day_close = float(df["low"].iloc[i]), float(df["high"].iloc[i]), float(df["close"].iloc[i])
            exit_reason, exit_price = None, None
            if day_low <= open_trade["sl"]:
                exit_reason, exit_price = "stop_loss", open_trade["sl"]
            elif day_high >= open_trade["t1"]:
                exit_reason, exit_price = "target1_hit", open_trade["t1"]
            elif days_held >= MAX_HOLDING_DAYS:
                exit_reason, exit_price = "timeout", day_close
            if exit_reason:
                entry = open_trade["entry"]
                outcome_pct = round((exit_price - entry) / entry * 100, 2)
                trades.append({
                    "symbol": symbol, "sector": sector,
                    "entry_date": str(df.index[open_trade["entry_idx"]].date()),
                    "exit_date": str(df.index[i].date()),
                    "entry": round(entry, 2), "exit": round(exit_price, 2),
                    "outcome_pct": outcome_pct, "exit_reason": exit_reason,
                    "hold_days": days_held,
                    "voters_at_entry": open_trade["voters_at_entry"],
                    "consensus_at_entry": open_trade["consensus"],
                })
                open_trade = None
            continue

        v = _v_at(ind, i)
        if v is None:
            continue
        strategies = {
            "Inna": _strategy_inna(v),
            "Pham": _strategy_pham(v, peg=None),  # point 2
            "Cianni": _strategy_cianni(v),
            "Unger": _strategy_unger(v),
        }
        consensus = _weighted_signal(strategies)
        if consensus not in ("BUY", "STRONG_BUY"):
            continue

        entry = v["spot"]
        atr = v["atr"] or entry * 0.02
        sl = round(entry - 1.5 * atr, 2)
        risk = max(entry - sl, 0.01)
        t1 = round(entry + 1.25 * risk, 2)
        voters_at_entry = [name for name, sig in strategies.items() if sig in ("BUY", "STRONG_BUY")]
        open_trade = {"entry_idx": i, "entry": entry, "sl": sl, "t1": t1,
                      "voters_at_entry": voters_at_entry, "consensus": consensus}

    return trades


def _trade_stats(trades):
    if not trades:
        return {"count": 0, "win_rate": None, "avg_return_pct": None,
                "sharpe_approx": None, "max_drawdown_pct": None,
                "hit_rate_pct": None, "avg_hold_days": None}
    returns = [t["outcome_pct"] for t in trades]
    wins = [r for r in returns if r > 0]
    win_rate = round(len(wins) / len(returns) * 100, 1)
    avg_return = round(sum(returns) / len(returns), 2)

    r = np.array(returns) / 100.0
    sharpe = None
    if len(r) >= 2 and r.std(ddof=1) > 0:
        span_days = max((pd.to_datetime(trades[-1]["exit_date"]) - pd.to_datetime(trades[0]["entry_date"])).days, 1)
        trades_per_year = len(trades) / (span_days / 365.25)
        sharpe = round((r.mean() / r.std(ddof=1)) * np.sqrt(max(trades_per_year, 1)), 2)

    equity = np.cumprod(1 + r)
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = round(float(drawdown.min()) * 100, 2) if len(drawdown) else None

    avg_hold = round(sum(t["hold_days"] for t in trades) / len(trades), 1)

    return {
        "count": len(trades), "win_rate": win_rate, "avg_return_pct": avg_return,
        "sharpe_approx": sharpe, "max_drawdown_pct": max_dd,
        "hit_rate_pct": win_rate, "avg_hold_days": avg_hold,
    }


def _buyhold_benchmark(price_data):
    """Equal-weighted buy-and-hold across the whole universe over the same
    window each stock actually had data for -- approximate, not apples-to-
    apples with the strategy (buy-and-hold is always-invested in all 46;
    the strategy is opportunistic/sporadic). See docstring."""
    per_stock_returns = []
    for symbol, df in price_data.items():
        if len(df) < 2:
            continue
        start, end = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
        years = max((df.index[-1] - df.index[0]).days / 365.25, 0.1)
        cagr = (end / start) ** (1 / years) - 1
        per_stock_returns.append(cagr)
    if not per_stock_returns:
        return None
    return round(sum(per_stock_returns) / len(per_stock_returns) * 100, 2)


def main():
    sector_map = _ticker_sector_map()
    all_trades = []
    price_data = {}
    errors = {}

    print(f"[{now_ist_str()}] Backtest starting -- {len(sector_map)} stocks, period={BACKTEST_PERIOD}")
    for symbol, sector in sector_map.items():
        ticker = symbol + ".NS"
        try:
            df = download_with_retry(ticker, period=BACKTEST_PERIOD)
            if df.empty or len(df) < MIN_BARS_WARMUP + 10:
                errors[symbol] = "insufficient price history"
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            price_data[symbol] = df
            trades = _simulate_stock(symbol, sector, df)
            all_trades.extend(trades)
            print(f"  {symbol}: {len(trades)} simulated trade(s)")
        except Exception as e:
            errors[symbol] = str(e)
            print(f"  {symbol} ERROR: {e}")

    overall_stats = _trade_stats(all_trades)
    per_voter_stats = {}
    for voter in VOTERS:
        voter_trades = [t for t in all_trades if voter in t["voters_at_entry"]]
        per_voter_stats[voter] = _trade_stats(voter_trades)

    buyhold_pct = _buyhold_benchmark(price_data)

    result = {
        "run_at": now_ist_str(),
        "period": BACKTEST_PERIOD,
        "universe_size": len(sector_map),
        "universe_with_data": len(price_data),
        "errors": errors,
        "overall": overall_stats,
        "by_voter": per_voter_stats,
        "buy_and_hold_annualized_pct": buyhold_pct,
        "assumptions": {
            "max_holding_days": MAX_HOLDING_DAYS,
            "exit_rule": "SL or T1 or max_holding_days timeout, whichever first -- invented for this backtest, not an extracted live rule (see module docstring point 4)",
            "voter_weights": "flat equal (0.25 each) for the entire window, not a retroactive replay of voter_weights_refresh.py's learning (point 3)",
            "peg_ratio": "not backtested -- no historical PEG source; Pham tested on technical half only (point 2)",
            "sharpe_method": "trade-return mean/stdev annualized by trades-per-year, NOT a NAV/equity-curve Sharpe (point 7)",
            "max_drawdown_method": "chronological compounding of closed trades as discrete full-capital bets, does not model concurrent multi-position capital split (point 7)",
        },
        "trades": all_trades,
        "disclaimer": "Backtest of a strategy reconstruction (see equity_scan_core.py's own disclaimer) against real historical yfinance daily data. Educational validation only -- past performance of a re-derived signal set is not evidence of future results, and several methodology simplifications are disclosed in 'assumptions' above. This is Gate 2 validation per the Master Brief's build order; it does not authorize wiring any executor.",
    }

    OUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print()
    print(f"  Overall: {overall_stats['count']} trades, {overall_stats['win_rate']}% win rate, "
          f"avg {overall_stats['avg_return_pct']}%, Sharpe~{overall_stats['sharpe_approx']}, "
          f"max DD {overall_stats['max_drawdown_pct']}%")
    for voter, s in per_voter_stats.items():
        print(f"  {voter}: {s['count']} trades, {s['win_rate']}% win rate, avg {s['avg_return_pct']}%")
    print(f"  Buy-and-hold (equal-weighted, annualized): {buyhold_pct}%")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
