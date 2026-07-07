"""
Recommendation Journal + Outcome Tracker (spec items 1 & 2 of the
"learning engine" roadmap).

WHAT THIS ADDS THAT DIDN'T EXIST: the system could already paper-trade
NIFTY/BANKNIFTY F&O (trade_brain.py) and score closed EQUITY trades the
user manually took (voter_weights_refresh.py), but there was no single
place tracking the outcome of EVERY actionable recommendation the engine
emits -- across index F&O, stock F&O, AND equity -- whether or not the user
acted on it. That's the point of this module: score the SIGNALS themselves,
so we learn what actually works before trusting any of it with capital.

RELATIONSHIP TO EXISTING CODE (not a duplicate, a superset observer):
  - trade_brain.py stays the F&O paper-trade engine (one open sim position
    per index, marks to market, feeds trade_journal.json / the dashboard's
    Trading Brain panel). This tracker is broader + lighter: it records the
    recommendation, marks it, and scores win/lost/expired/invalidated, but
    does not replace trade_brain's richer per-instrument sim.
  - voter_weights_refresh.py still learns per-voter weights from real closed
    equity trades. The per-vote-count / per-regime stats here are additional
    context over the recommendation set, not a replacement.
  - backtest.py is the HISTORICAL validation; this is the FORWARD one.

HONEST SCOPE / KNOWN LIMITS (labeled, not hidden -- same discipline as the
rest of this repo):
  - F&O outcomes use _premium_estimate() (refresh_fo_cloud.py) -- the SAME
    estimated-premium model used everywhere else here. There is no real
    NSE option-chain feed anywhere in this project, so every F&O entry/mark
    is an ESTIMATE, flagged via entry_basis="premium_estimate". ann_vol is
    captured at open time and reused for re-pricing (realized vol is
    slow-moving; refreshing it per run would add noise for little gain) --
    documented approximation, not a hidden one.
  - Stock-F&O SPREAD recommendations (votes 4-5, two legs) are RECORDED but
    scored only coarsely: won/lost decided at/after expiry by the underlying
    spot vs the spread's stored breakeven (entry_basis=
    "spread_underlying_vs_breakeven"). Proper two-leg time-value P&L is
    deliberately NOT modeled here -- half-building it would fabricate
    numbers. Single-leg F&O (index + max-conviction stock) and equity are
    tracked fully.
  - Equity recommendations re-mark only when equity_scan.json refreshes
    (once daily, 9pm prep cron) -- intraday runs can't re-price equities
    (no live per-stock equity feed on the 5-min cadence). Fine for swing
    signals; documented.
  - "false flip" metric from the spec (did an invalidated rec later work
    anyway?) is NOT computed yet -- it needs post-invalidation price
    tracking beyond a rec's own close. Deferred with a TODO rather than
    approximated. The invalidated-count IS tracked, which is the input to it.

Anti-hallucination rules enforced (all already house style): no data -> no
rec; source recorded on every rec; estimated premium labeled estimated;
never fabricate an option-chain price or a trade outcome; now_ist_str()
only; utf-8 writes; no emoji in source/print.

Output: recommendation_journal.json at repo root (a real dashboard feed --
registered in validate_json_outputs.py SCHEMA and vercel.json). Runs each
refresh, after the signal files it reads are freshly written.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

from ist_time import now_ist, now_ist_str
from refresh_fo_cloud import _premium_estimate

REPO_ROOT = pathlib.Path(__file__).parent.parent
JOURNAL_FILE = REPO_ROOT / "recommendation_journal.json"
FO_FILE = REPO_ROOT / "fo_latest.json"
STOCK_FO_FILE = REPO_ROOT / "stock_fo.json"
EQUITY_SCAN_FILE = REPO_ROOT / "equity_scan.json"
REGIME_FILE = REPO_ROOT / "market_regime.json"

JOURNAL_MAX_CLOSED = 2000        # keep all open + most recent N closed
EQUITY_MAX_HOLD_DAYS = 30        # calendar days ~= 20 trading days, matches backtest.py's convention


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_ist(ts):
    """'07 Jul 2026 17:56 IST' -> naive datetime (for age math). None on failure."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts.replace(" IST", ""), "%d %b %Y %H:%M")
    except Exception:
        return None


def _expiry_days_remaining(expiry_str):
    """'30 Jul 2026' -> integer days from today (IST). None if unparseable."""
    try:
        exp = datetime.strptime(expiry_str, "%d %b %Y").date()
        return (exp - now_ist().date()).days
    except Exception:
        return None


def _regime_for(kind, symbol, regime):
    """Market-context trend at rec time. F&O index uses its own; stock F&O
    and equity use NIFTY50 as the broad-market reference (documented choice,
    not a claim that every stock tracks NIFTY)."""
    instruments = (regime or {}).get("instruments", {})
    if kind == "fo_index" and symbol in instruments:
        return instruments[symbol].get("trend")
    return (instruments.get("NIFTY50") or {}).get("trend")


# --------------------------------------------------------------------------
# Opening new recommendations from the freshly-written signal files
# --------------------------------------------------------------------------

def _open_key(rec):
    return (rec["kind"], rec["symbol"], rec["direction"])


def _collect_current_recs(fo, stock_fo, equity, regime, now_str):
    """Every CURRENTLY-actionable signal, as a candidate recommendation.
    Dedup against already-open recs happens in main()."""
    out = []

    # --- F&O index (NIFTY50 / BANKNIFTY): always single-leg CE/PE ---
    for sym in ("NIFTY50", "BANKNIFTY"):
        sig = (fo or {}).get(sym)
        if not isinstance(sig, dict) or "error" in sig:
            continue
        consensus = sig.get("consensus")
        trade = sig.get("trade") or {}
        if consensus not in ("BUY_CE", "BUY_PE") or not trade.get("strike"):
            continue
        opt = "CE" if consensus == "BUY_CE" else "PE"
        out.append({
            "id": f"foidx-{sym}-{consensus}-{now_str.replace(' ', '').replace(':', '')}",
            "kind": "fo_index", "symbol": sym, "direction": consensus,
            "entry": trade.get("entry_premium"), "entry_basis": "premium_estimate",
            "entry_spot": sig.get("spot"),
            "stop": trade.get("sl_premium"),
            "targets": [t for t in (trade.get("target1_premium"), trade.get("target_premium")) if t is not None],
            "strike": trade.get("strike"), "option_type": opt,
            "ann_vol_at_open": sig.get("ann_vol"),
            "expiry": trade.get("expiry"),
            "vote_count": sig.get("ce_votes") if consensus == "BUY_CE" else sig.get("pe_votes"),
            "voters": None,  # index engine is a 3-factor score, not the 4 named voters
            "market_regime": _regime_for("fo_index", sym, regime),
            "data_source": sig.get("data_source"),
            "opened_at": now_str, "status": "open",
            "close_reason": None, "closed_at": None, "exit": None,
            "outcome_pct": None, "current": trade.get("entry_premium"), "last_checked": now_str,
        })

    # --- Stock F&O: single-leg (votes==6) tracked fully; spread recorded ---
    for sym, sig in (stock_fo or {}).items():
        if sym == "_meta" or not isinstance(sig, dict):
            continue
        consensus = sig.get("consensus")
        trade = sig.get("trade")
        if consensus not in ("BUY_CE", "BUY_PE") or not isinstance(trade, dict):
            continue
        opt = "CE" if consensus == "BUY_CE" else "PE"
        base = {
            "symbol": sym, "direction": consensus, "entry_spot": sig.get("spot"),
            "option_type": opt, "expiry": trade.get("expiry"),
            "vote_count": sig.get("votes"), "voters": None,
            "market_regime": _regime_for("fo_stock", sym, regime),
            "data_source": sig.get("data_source"),
            "opened_at": now_str, "status": "open",
            "close_reason": None, "closed_at": None, "exit": None, "outcome_pct": None,
            "last_checked": now_str,
        }
        if trade.get("type") == "single_leg":
            out.append({**base,
                "id": f"fostk-{sym}-{consensus}-{now_str.replace(' ', '').replace(':', '')}",
                "kind": "fo_stock", "entry": trade.get("entry_premium"),
                "entry_basis": "premium_estimate",
                "stop": trade.get("sl_premium"),
                "targets": [t for t in (trade.get("target1_premium"), trade.get("target2_premium")) if t is not None],
                "strike": trade.get("strike"),
                "ann_vol_at_open": sig.get("ann_vol"),  # now surfaced by stock_fo_refresh.py, so single-leg stock F&O re-prices like the index
                "current": trade.get("entry_premium"),
            })
        else:  # spread
            out.append({**base,
                "id": f"fospr-{sym}-{consensus}-{now_str.replace(' ', '').replace(':', '')}",
                "kind": "fo_stock_spread", "entry": trade.get("net_debit"),
                "entry_basis": "spread_underlying_vs_breakeven",
                "stop": None, "targets": None,
                "long_strike": trade.get("long_strike"), "short_strike": trade.get("short_strike"),
                "breakeven": trade.get("breakeven"), "max_profit": trade.get("max_profit"),
                "max_loss": trade.get("max_loss"),
                "current": None,
            })

    # --- Equity STRONG_BUY / BUY ---
    for sym, sig in (equity or {}).items():
        if sym == "_meta" or not isinstance(sig, dict):
            continue
        signal = sig.get("signal")
        if signal not in ("BUY", "STRONG_BUY"):
            continue
        strategies = sig.get("strategies", {})
        voters = [n for n, v in strategies.items() if v in ("BUY", "STRONG_BUY")]
        out.append({
            "id": f"eq-{sym}-{signal}-{now_str.replace(' ', '').replace(':', '')}",
            "kind": "equity", "symbol": sym, "direction": signal,
            "entry": sig.get("entry"), "entry_basis": "spot_price",
            "entry_spot": sig.get("entry"),
            "stop": sig.get("sl"),
            "targets": [t for t in (sig.get("t1"), sig.get("t2"), sig.get("t3")) if t is not None],
            "strike": None, "option_type": None, "ann_vol_at_open": None, "expiry": None,
            "vote_count": sig.get("buy_votes"), "voters": voters,
            "market_regime": _regime_for("equity", sym, regime),
            "data_source": sig.get("data_source"),
            "opened_at": now_str, "status": "open",
            "close_reason": None, "closed_at": None, "exit": None,
            "outcome_pct": None, "current": sig.get("entry"), "last_checked": now_str,
        })

    return out


# --------------------------------------------------------------------------
# Marking open recommendations to market + closing them
# --------------------------------------------------------------------------

def _current_consensus(rec, fo, stock_fo, equity):
    if rec["kind"] == "fo_index":
        return ((fo or {}).get(rec["symbol"]) or {}).get("consensus")
    if rec["kind"] in ("fo_stock", "fo_stock_spread"):
        return ((stock_fo or {}).get(rec["symbol"]) or {}).get("consensus")
    if rec["kind"] == "equity":
        return ((equity or {}).get(rec["symbol"]) or {}).get("signal")
    return None


def _current_spot(rec, fo, stock_fo, equity):
    if rec["kind"] == "fo_index":
        return ((fo or {}).get(rec["symbol"]) or {}).get("spot")
    if rec["kind"] in ("fo_stock", "fo_stock_spread"):
        return ((stock_fo or {}).get(rec["symbol"]) or {}).get("spot")
    if rec["kind"] == "equity":
        return ((equity or {}).get(rec["symbol"]) or {}).get("entry")
    return None


def _close(rec, status, reason, exit_val, now_str):
    rec["status"] = status
    rec["close_reason"] = reason
    rec["exit"] = exit_val
    rec["closed_at"] = now_str
    entry = rec.get("entry")
    rec["outcome_pct"] = round((exit_val - entry) / entry * 100, 2) if entry else None
    rec["last_checked"] = now_str


def _flip_is_against(rec, consensus):
    """Has the live signal turned against this recommendation?"""
    if rec["kind"] == "equity":
        return consensus not in ("BUY", "STRONG_BUY")
    # F&O: anything that isn't the same actionable direction = against
    return consensus != rec["direction"]


def _mark_open_rec(rec, fo, stock_fo, equity, now_str):
    cur_spot = _current_spot(rec, fo, stock_fo, equity)
    consensus = _current_consensus(rec, fo, stock_fo, equity)

    # --- single-leg F&O (index + stock): re-estimate premium ---
    if rec["kind"] in ("fo_index", "fo_stock"):
        days_rem = _expiry_days_remaining(rec.get("expiry")) if rec.get("expiry") else None
        if cur_spot is not None and rec.get("strike") and rec.get("ann_vol_at_open") is not None and days_rem is not None:
            premium = _premium_estimate(cur_spot, rec["strike"], rec["ann_vol_at_open"], max(days_rem, 1), rec["option_type"])
            rec["current"] = premium
            targets = rec.get("targets") or []
            if targets and premium >= targets[0]:
                return _close(rec, "won", "target1_hit", premium, now_str)
            if rec.get("stop") is not None and premium <= rec["stop"]:
                return _close(rec, "lost", "stop_loss", premium, now_str)
            if days_rem <= 0:
                return _close(rec, "expired", "expired", premium, now_str)
        # ann_vol_at_open None (stock F&O doesn't expose it) -> can't re-price;
        # fall through to signal-flip / expiry checks below on underlying only.
        if consensus is not None and _flip_is_against(rec, consensus):
            return _close(rec, "invalidated", "signal_invalidated", rec.get("current"), now_str)
        if days_rem is not None and days_rem <= 0:
            return _close(rec, "expired", "expired", rec.get("current"), now_str)
        rec["last_checked"] = now_str
        return

    # --- stock F&O spread: underlying vs breakeven, settles at expiry ---
    if rec["kind"] == "fo_stock_spread":
        days_rem = _expiry_days_remaining(rec.get("expiry")) if rec.get("expiry") else None
        if cur_spot is not None:
            rec["current"] = cur_spot
        if consensus is not None and _flip_is_against(rec, consensus):
            pct = round((cur_spot - rec["entry_spot"]) / rec["entry_spot"] * 100, 2) if (cur_spot and rec.get("entry_spot")) else None
            rec["status"], rec["close_reason"], rec["exit"] = "invalidated", "signal_invalidated", cur_spot
            rec["closed_at"], rec["outcome_pct"], rec["last_checked"] = now_str, pct, now_str
            return
        if days_rem is not None and days_rem <= 0 and cur_spot is not None and rec.get("breakeven") is not None:
            favorable = cur_spot >= rec["breakeven"] if rec["direction"] == "BUY_CE" else cur_spot <= rec["breakeven"]
            pct = round((cur_spot - rec["entry_spot"]) / rec["entry_spot"] * 100, 2) if rec.get("entry_spot") else None
            rec["status"] = "won" if favorable else "lost"
            rec["close_reason"], rec["exit"] = "expired_settled_vs_breakeven", cur_spot
            rec["closed_at"], rec["outcome_pct"], rec["last_checked"] = now_str, pct, now_str
            return
        rec["last_checked"] = now_str
        return

    # --- equity: spot-based, daily granularity ---
    if rec["kind"] == "equity":
        if cur_spot is not None:
            rec["current"] = cur_spot
            targets = rec.get("targets") or []
            if targets and cur_spot >= targets[0]:
                return _close(rec, "won", "target1_hit", cur_spot, now_str)
            if rec.get("stop") is not None and cur_spot <= rec["stop"]:
                return _close(rec, "lost", "stop_loss", cur_spot, now_str)
        if consensus is not None and _flip_is_against(rec, consensus):
            return _close(rec, "invalidated", "signal_invalidated", rec.get("current"), now_str)
        opened = _parse_ist(rec.get("opened_at"))
        if opened is not None:
            days_held = (now_ist().replace(tzinfo=None) - opened).days
            if days_held >= EQUITY_MAX_HOLD_DAYS:
                return _close(rec, "expired", "max_hold_exceeded", rec.get("current"), now_str)
        rec["last_checked"] = now_str
        return


# --------------------------------------------------------------------------
# Summary stats (a LIGHT version -- the full strategy_performance.py learning
# engine is a separate, later module; this is just enough to be useful now)
# --------------------------------------------------------------------------

def _bucket_winrate(recs, keyfn):
    out = {}
    for r in recs:
        if r["status"] not in ("won", "lost"):
            continue
        k = keyfn(r)
        if k is None:
            continue
        b = out.setdefault(str(k), {"won": 0, "lost": 0})
        b["won" if r["status"] == "won" else "lost"] += 1
    for b in out.values():
        total = b["won"] + b["lost"]
        b["win_rate"] = round(b["won"] / total * 100, 1) if total else None
        b["total"] = total
    return out


def _summary(journal):
    closed = [r for r in journal if r["status"] in ("won", "lost", "expired", "invalidated")]
    decisive = [r for r in closed if r["status"] in ("won", "lost")]
    won = [r for r in decisive if r["status"] == "won"]
    win_rate = round(len(won) / len(decisive) * 100, 1) if decisive else None
    returns = [r["outcome_pct"] for r in decisive if r.get("outcome_pct") is not None]
    avg_return = round(sum(returns) / len(returns), 2) if returns else None
    return {
        "open_count": sum(1 for r in journal if r["status"] == "open"),
        "closed_count": len(closed),
        "decisive_count": len(decisive),
        "won": len(won),
        "lost": len(decisive) - len(won),
        "expired_count": sum(1 for r in closed if r["status"] == "expired"),
        "invalidated_count": sum(1 for r in closed if r["status"] == "invalidated"),
        "win_rate_pct": win_rate,
        "avg_return_pct": avg_return,
        "by_kind": _bucket_winrate(decisive, lambda r: r["kind"]),
        "by_vote_count": _bucket_winrate(decisive, lambda r: r.get("vote_count")),
        "by_regime": _bucket_winrate(decisive, lambda r: r.get("market_regime")),
        "false_flip_rate_pct": None,  # TODO -- see module docstring; needs post-invalidation tracking
    }


def main():
    now_str = now_ist_str()
    fo = _load_json(FO_FILE, {})
    stock_fo = _load_json(STOCK_FO_FILE, {})
    equity = _load_json(EQUITY_SCAN_FILE, {})
    regime = _load_json(REGIME_FILE, {})

    journal_data = _load_json(JOURNAL_FILE, {})
    journal = journal_data.get("recommendations", []) if isinstance(journal_data, dict) else []
    if not isinstance(journal, list):
        journal = []

    # 1) mark every open rec to market (may close some)
    open_recs = [r for r in journal if r.get("status") == "open"]
    for r in open_recs:
        _mark_open_rec(r, fo, stock_fo, equity, now_str)

    # 2) open new recs for currently-actionable signals not already open
    still_open_keys = {_open_key(r) for r in journal if r.get("status") == "open"}
    opened_now = 0
    for cand in _collect_current_recs(fo, stock_fo, equity, regime, now_str):
        if _open_key(cand) not in still_open_keys:
            journal.append(cand)
            still_open_keys.add(_open_key(cand))
            opened_now += 1

    # 3) cap: keep all open + most recent JOURNAL_MAX_CLOSED closed
    open_part = [r for r in journal if r.get("status") == "open"]
    closed_part = [r for r in journal if r.get("status") != "open"]
    closed_part.sort(key=lambda r: r.get("closed_at") or "")
    journal = open_part + closed_part[-JOURNAL_MAX_CLOSED:]

    result = {
        "generated_at": now_str,
        "summary": _summary(journal),
        "recommendations": journal,
        "disclaimer": "Virtual recommendation track record -- scores the engine's OWN signals, "
                      "not trades the user took. F&O marks are estimated premiums (no real "
                      "option-chain feed exists here); spreads are scored coarsely vs breakeven "
                      "at expiry. See recommendation_tracker.py docstring for full method + limits. "
                      "Educational only, not investment advice.",
    }
    JOURNAL_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    s = result["summary"]
    print(f"  opened {opened_now} new rec(s) this run | {s['open_count']} open, {s['decisive_count']} decisive "
          f"({s['won']}W/{s['lost']}L, win_rate={s['win_rate_pct']}%), "
          f"{s['expired_count']} expired, {s['invalidated_count']} invalidated")
    print(f"  Wrote {JOURNAL_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never break the refresh chain over a tracking problem.
        print(f"  ERROR in recommendation_tracker main(): {e} -- journal left unchanged this run")
