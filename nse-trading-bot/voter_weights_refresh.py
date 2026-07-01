"""
Nightly per-voter weight recalculation — rolling win rate + expectancy per
strategy voter (Inna/Pham/Cianni/Unger) over their last closed equity
trades, normalized into weights summing to 1. Writes voter_weights.json,
which equity_scan_core.py reads to weight its consensus scoring instead of
treating all 4 voters as equally reliable by default.

Data source: my_positions.json's closedTrades array (written by the
dashboard via api/save-positions.js whenever a position is removed with an
exit price — see removePosition() in nse_live_dashboard.html). Each closed
trade records which specific voters recommended it (BUY/STRONG_BUY) at
entry time, so a voter's outcomes can be isolated from the others' even
though they usually overlap on the same trades.

Honesty note: with only a handful of real closed equity trades so far
(this is brand new infrastructure as of today), every voter will show
"insufficient data" and default to equal weight (0.25 each) for a long
while — that's the correct behavior, not a bug. Forcing a confident-looking
weight split from 3 trades would be noise dressed up as signal.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
POSITIONS_FILE = REPO_ROOT / "my_positions.json"
OUT_FILE = REPO_ROOT / "voter_weights.json"

VOTERS = ["Inna", "Pham", "Cianni", "Unger"]
LOOKBACK_TRADES = 60   # per-voter rolling window, per user's TASKS.md ("last 30-60 closed trades")
MIN_TRADES_FOR_SIGNAL = 5  # below this, a voter's win rate is noise, not signal
MIN_WEIGHT_FLOOR = 0.05    # no voter's weight goes to exactly 0 off a bad early run


def _load_closed_trades():
    if not POSITIONS_FILE.exists():
        return []
    try:
        data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
        trades = data.get("closedTrades", []) if isinstance(data, dict) else []
        return trades if isinstance(trades, list) else []
    except Exception:
        return []


def _voter_stats(trades, voter):
    """Trades where this voter said BUY/STRONG_BUY at entry, most recent
    LOOKBACK_TRADES of them, with win rate + avg return."""
    relevant = [
        t for t in trades
        if isinstance(t.get("strategies_at_buy"), dict)
        and t["strategies_at_buy"].get(voter) in ("BUY", "STRONG_BUY")
        and t.get("outcome_pct") is not None
    ]
    relevant = relevant[-LOOKBACK_TRADES:]
    if len(relevant) < MIN_TRADES_FOR_SIGNAL:
        return {
            "trade_count": len(relevant),
            "win_rate": None,
            "avg_return_pct": None,
            "sufficient_data": False,
        }
    wins = [t for t in relevant if t["outcome_pct"] > 0]
    win_rate = round(len(wins) / len(relevant) * 100, 1)
    avg_return = round(sum(t["outcome_pct"] for t in relevant) / len(relevant), 2)
    return {
        "trade_count": len(relevant),
        "win_rate": win_rate,
        "avg_return_pct": avg_return,
        "sufficient_data": True,
    }


def _recompute_weights(stats):
    any_sufficient = any(s["sufficient_data"] for s in stats.values())
    if not any_sufficient:
        n = len(VOTERS)
        return {v: round(1 / n, 4) for v in VOTERS}, "insufficient data for all voters — using equal weights"

    # Voters with enough data score on their real win rate (floored so a rough
    # patch doesn't zero them out); voters still building a sample get the
    # average of the voters that DO have signal, as a neutral placeholder
    # rather than arbitrarily 0 or 100.
    known_rates = [s["win_rate"] for s in stats.values() if s["sufficient_data"]]
    neutral_rate = sum(known_rates) / len(known_rates)

    raw = {}
    for v in VOTERS:
        s = stats[v]
        rate = s["win_rate"] if s["sufficient_data"] else neutral_rate
        raw[v] = max(rate, MIN_WEIGHT_FLOOR * 100)

    total = sum(raw.values())
    weights = {v: round(raw[v] / total, 4) for v in VOTERS}
    return weights, "computed from real closed-trade outcomes"


def main():
    try:
        trades = _load_closed_trades()
        stats = {v: _voter_stats(trades, v) for v in VOTERS}
        weights, method = _recompute_weights(stats)

        result = {
            "computed_at": now_ist_str(),
            "total_closed_trades": len(trades),
            "lookback_trades": LOOKBACK_TRADES,
            "voters": {
                v: {**stats[v], "weight": weights[v]}
                for v in VOTERS
            },
            "method": method,
            "disclaimer": "Weights only diverge from equal (0.25 each) once a voter has at least "
                           f"{MIN_TRADES_FOR_SIGNAL} closed trades with a real outcome — a handful of "
                           "trades isn't enough to distinguish real edge from noise.",
        }
        OUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"  {len(trades)} total closed trades, method: {method}")
        for v in VOTERS:
            s = stats[v]
            print(f"  {v}: weight={weights[v]} trades={s['trade_count']} win_rate={s['win_rate']}")
        print(f"  Wrote {OUT_FILE}")
    except Exception as e:
        OUT_FILE.write_text(json.dumps({
            "computed_at": now_ist_str(),
            "error": str(e),
            "voters": {v: {"weight": round(1/len(VOTERS), 4)} for v in VOTERS},
        }, indent=2), encoding="utf-8")
        print(f"  ERROR in main(): {e} — wrote equal-weight fallback")


if __name__ == "__main__":
    main()
