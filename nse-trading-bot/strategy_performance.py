"""
Strategy Performance report (learning-engine roadmap item 4) — the unified
"what actually works" read over recommendation_journal.json's CLOSED recs.

WHY A NEW MODULE AND NOT JUST REUSE WHAT EXISTS: three places already compute
win-rate-ish stats, each over DIFFERENT data --
  - trade_brain.py::_compute_stats() over F&O paper trades (trade_journal.json)
  - voter_weights_refresh.py over real closed EQUITY trades (my_positions.json)
  - backtest.py over 3y HISTORICAL replay
This report unifies over the FORWARD recommendation journal (every signal the
engine emitted, all kinds), which none of the above cover. It deliberately
does NOT recompute or replace those three -- it's the cross-cutting scoreboard
over the new journal, sliced the way the "learning engine" spec asked for:
by kind, vote count, direction, regime, voter, and time-of-day.

READ-ONLY. No behavior change anywhere -- it consumes recommendation_journal
.json and writes strategy_performance.json (a dashboard feed). Nothing
downstream acts on it yet; it's the evidence base a human (or a future
adaptive-scoring module) reads before trusting or down-ranking any setup.

Method notes (kept consistent with backtest.py so numbers are comparable):
  - expectancy = mean outcome_pct across decisive (won|lost) recs.
  - profit_factor = sum(win %) / abs(sum(loss %)); None if no losses.
  - max_drawdown compounds decisive recs in close-order as discrete
    full-stake bets -- overstates risk vs a real split-capital book, same
    caveat as backtest.py, disclosed not hidden.
  - "false flip" proper (did an invalidated rec later hit its target?) still
    needs post-invalidation tracking the journal doesn't do yet. Reported
    here is a labeled PROXY: invalidated_while_in_profit = we bailed on a rec
    that was green at the moment the signal flipped. Not the true metric.

Anti-hallucination: every number derives from real recorded outcomes; empty
input -> honest "no closed recommendations yet", never a fabricated stat.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
JOURNAL_FILE = REPO_ROOT / "recommendation_journal.json"
OUT_FILE = REPO_ROOT / "strategy_performance.json"


def _load_recs():
    try:
        d = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
        recs = d.get("recommendations", []) if isinstance(d, dict) else []
        return recs if isinstance(recs, list) else []
    except Exception:
        return []


def _hold_days(rec):
    try:
        o = datetime.strptime((rec.get("opened_at") or "").replace(" IST", ""), "%d %b %Y %H:%M")
        c = datetime.strptime((rec.get("closed_at") or "").replace(" IST", ""), "%d %b %Y %H:%M")
        return max((c - o).days, 0)
    except Exception:
        return None


def _hour_bucket(rec):
    try:
        o = datetime.strptime((rec.get("opened_at") or "").replace(" IST", ""), "%d %b %Y %H:%M")
        h = o.hour
        if h < 10: return "open (09-10)"
        if h < 12: return "morning (10-12)"
        if h < 14: return "midday (12-14)"
        if h < 16: return "afternoon (14-16)"
        return "off-hours"
    except Exception:
        return None


def _core_stats(recs):
    """recs: a list already filtered to what we want stats over. Only
    decisive (won|lost) recs count toward win rate / expectancy."""
    decisive = [r for r in recs if r.get("status") in ("won", "lost")]
    if not decisive:
        return {"decisive": 0, "won": 0, "lost": 0, "win_rate_pct": None,
                "expectancy_pct": None, "avg_win_pct": None, "avg_loss_pct": None,
                "profit_factor": None, "max_drawdown_pct": None}
    wins = [r for r in decisive if r["status"] == "won"]
    losses = [r for r in decisive if r["status"] == "lost"]
    win_returns = [r["outcome_pct"] for r in wins if r.get("outcome_pct") is not None]
    loss_returns = [r["outcome_pct"] for r in losses if r.get("outcome_pct") is not None]
    all_returns = [r["outcome_pct"] for r in decisive if r.get("outcome_pct") is not None]

    win_rate = round(len(wins) / len(decisive) * 100, 1)
    expectancy = round(sum(all_returns) / len(all_returns), 2) if all_returns else None
    avg_win = round(sum(win_returns) / len(win_returns), 2) if win_returns else None
    avg_loss = round(sum(loss_returns) / len(loss_returns), 2) if loss_returns else None
    gross_win = sum(win_returns)
    gross_loss = abs(sum(loss_returns))
    profit_factor = round(gross_win / gross_loss, 2) if gross_loss else None

    # chronological compounded max drawdown (same method/caveat as backtest.py)
    ordered = sorted(decisive, key=lambda r: r.get("closed_at") or "")
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in ordered:
        pct = r.get("outcome_pct")
        if pct is None:
            continue
        equity *= (1 + pct / 100.0)
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity - peak) / peak)

    return {"decisive": len(decisive), "won": len(wins), "lost": len(losses),
            "win_rate_pct": win_rate, "expectancy_pct": expectancy,
            "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
            "profit_factor": profit_factor, "max_drawdown_pct": round(max_dd * 100, 2)}


def _by(recs, keyfn):
    groups = {}
    for r in recs:
        if r.get("status") not in ("won", "lost"):
            continue
        for k in keyfn(r):
            if k is None:
                continue
            groups.setdefault(str(k), []).append(r)
    return {k: _core_stats(v) for k, v in sorted(groups.items())}


def main():
    recs = _load_recs()
    closed = [r for r in recs if r.get("status") in ("won", "lost", "expired", "invalidated")]
    decisive = [r for r in closed if r.get("status") in ("won", "lost")]

    invalidated = [r for r in closed if r.get("status") == "invalidated"]
    invalidated_in_profit = sum(1 for r in invalidated if (r.get("outcome_pct") or 0) > 0)

    hold_days = [d for d in (_hold_days(r) for r in decisive) if d is not None]
    avg_hold = round(sum(hold_days) / len(hold_days), 1) if hold_days else None

    result = {
        "generated_at": now_ist_str(),
        "recommendations_total": len(recs),
        "open": sum(1 for r in recs if r.get("status") == "open"),
        "closed": len(closed),
        "overall": _core_stats(decisive),
        "avg_hold_days": avg_hold,
        "by_kind": _by(decisive, lambda r: [r.get("kind")]),
        "by_vote_count": _by(decisive, lambda r: [r.get("vote_count")]),
        "by_direction": _by(decisive, lambda r: [r.get("direction")]),
        "by_regime": _by(decisive, lambda r: [r.get("market_regime")]),
        "by_voter": _by(decisive, lambda r: (r.get("voters") or [None])),
        "by_time_of_day": _by(decisive, lambda r: [_hour_bucket(r)]),
        # Added 09-Jul-2026 (learning-engine roadmap item 5, Phase B) -- reads
        # the macro_context_at_open field recommendation_tracker.py now
        # stamps on every new rec. _by() already skips None keys, so recs
        # opened BEFORE this change (no such field) are silently excluded
        # from this dimension, not counted or corrupted -- there is no
        # retroactive backfill, this only starts accumulating going forward.
        "by_macro_risk_level": _by(decisive, lambda r: [(r.get("macro_context_at_open") or {}).get("risk_level")]),
        "flip_diagnostics": {
            "expired_count": sum(1 for r in closed if r.get("status") == "expired"),
            "invalidated_count": len(invalidated),
            "invalidated_while_in_profit": invalidated_in_profit,
            "note": "invalidated_while_in_profit is a PROXY for false flips (bailed on a green rec when the "
                    "signal flipped) -- NOT the true 'did it later hit target' metric, which needs "
                    "post-invalidation tracking the journal doesn't do yet.",
        },
        "sample_size_warning": (
            "Fewer than 20 decisive recommendations -- these numbers are noise, not signal. "
            "Treat as 'still accumulating', same threshold discipline as the rest of this system."
            if len(decisive) < 20 else None
        ),
        "disclaimer": "Forward performance of the engine's OWN recommendations (recommendation_journal.json), "
                      "all kinds. F&O outcomes use estimated premiums; see recommendation_tracker.py for "
                      "per-kind scoring method + limits. Educational only, not investment advice.",
    }
    OUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    o = result["overall"]
    print(f"  {len(recs)} recs ({result['open']} open, {len(closed)} closed, {o['decisive']} decisive)")
    if o["decisive"]:
        print(f"  overall: {o['win_rate_pct']}% win, expectancy {o['expectancy_pct']}%, "
              f"PF {o['profit_factor']}, maxDD {o['max_drawdown_pct']}%")
    else:
        print("  no decisive recommendations yet -- report will populate as recs close")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  ERROR in strategy_performance main(): {e} -- report left unchanged this run")
