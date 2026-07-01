"""
Trading Brain — mock/paper-trading learning layer.

Every run: reads the freshly-computed signals in fo_latest.json. For each
instrument:
  - if no open paper trade exists and the signal is actionable (BUY_CE/BUY_PE),
    opens one (entry = the same Black-Scholes-style premium estimate used for
    real trade suggestions).
  - if an open paper trade exists, marks it to market using the current spot
    and re-estimates premium, then closes it on SL / target / expiry / signal
    flip — exactly what a disciplined human paper-trader would do by hand.

This is NOT machine learning — it does not adjust model weights. It is an
honest, append-only track record: win rate by instrument and by vote-strength,
so a human (or a future Claude session) can see empirically which setups this
signal engine actually gets right before risking real capital.

Runs after refresh_fo_cloud.py in the same CI step, reusing its signal math.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
from ist_time import now_ist_str
from refresh_fo_cloud import INSTRUMENTS, _next_monthly_expiry, _atm, _premium_estimate

REPO_ROOT = pathlib.Path(__file__).parent.parent
SIGNAL_FILE = REPO_ROOT / "fo_latest.json"
JOURNAL_FILE = REPO_ROOT / "trade_journal.json"

MIN_VOTES = 4  # matches the BUY_CE/BUY_PE consensus threshold in refresh_fo_cloud.py


def _load_journal():
    if JOURNAL_FILE.exists():
        try:
            return json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"trades": [], "stats": {}}


def _open_trade(inst_name, sig, now_str):
    t = sig["trade"]
    consensus = sig["consensus"]
    opt = "CE" if consensus == "BUY_CE" else "PE"
    votes = sig["ce_votes"] if consensus == "BUY_CE" else sig["pe_votes"]
    return {
        "id": f"{inst_name}-{date.today().isoformat()}-{consensus}-{now_str.replace(' ', '').replace(':', '')}",
        "instrument": inst_name,
        "consensus": consensus,
        "option_type": opt,
        "votes": f"{votes}/5",
        "votes_n": votes,
        "strike": t["strike"],
        "expiry": t["expiry"],
        "entry_spot": sig["spot"],
        "entry_premium": t["entry_premium"],
        "sl_premium": t["sl_premium"],
        "target1_premium": t["target1_premium"],
        "target2_premium": t["target_premium"],
        "opened_at": now_str,
        "status": "open",
        "current_premium": t["entry_premium"],
        "closed_at": None,
        "exit_premium": None,
        "exit_reason": None,
        "pnl_pct": None,
    }


def _mark_to_market(trade, sig, now_str):
    """Re-price an open paper trade and close it if SL/target/expiry/flip triggers."""
    cfg = INSTRUMENTS[trade["instrument"]]
    expiry_dt = _next_monthly_expiry(cfg["expiry_day"])
    days_remaining = (expiry_dt - date.today()).days

    cur_premium = _premium_estimate(sig["spot"], trade["strike"], sig["ann_vol"], max(days_remaining, 1))
    trade["current_premium"] = cur_premium
    trade["last_checked"] = now_str

    def close(reason, exit_premium):
        trade["status"] = "closed"
        trade["closed_at"] = now_str
        trade["exit_premium"] = exit_premium
        trade["exit_reason"] = reason
        trade["pnl_pct"] = round((exit_premium - trade["entry_premium"]) / trade["entry_premium"] * 100, 1) if trade["entry_premium"] else 0.0
        trade["result"] = "win" if trade["pnl_pct"] > 0 else "loss"

    if cur_premium >= trade["target2_premium"]:
        close("target2_hit", cur_premium)
    elif cur_premium >= trade["target1_premium"]:
        close("target1_hit", cur_premium)
    elif cur_premium <= trade["sl_premium"]:
        close("stop_loss", cur_premium)
    elif days_remaining <= 0:
        close("expired", cur_premium)
    elif sig["consensus"] not in ("WAIT",) and sig["consensus"] != trade["consensus"]:
        close("signal_flip", cur_premium)
    # else: still open, just mark-to-market updated


def _compute_stats(trades):
    closed = [t for t in trades if t["status"] == "closed"]
    open_ = [t for t in trades if t["status"] == "open"]
    wins = [t for t in closed if t["result"] == "win"]
    losses = [t for t in closed if t["result"] == "loss"]

    def bucket(items, keyfn):
        out = {}
        for t in items:
            k = keyfn(t)
            b = out.setdefault(k, {"total": 0, "wins": 0})
            b["total"] += 1
            if t["result"] == "win":
                b["wins"] += 1
        for b in out.values():
            b["win_rate"] = round(b["wins"] / b["total"] * 100, 1) if b["total"] else 0.0
        return out

    by_instrument = bucket(closed, lambda t: t["instrument"])
    by_votes = bucket(closed, lambda t: t["votes"])

    insight = "Not enough closed trades yet to draw a conclusion."
    if len(closed) >= 3:
        best_votes = max(by_votes.items(), key=lambda kv: (kv[1]["win_rate"], kv[1]["total"]), default=None)
        if best_votes:
            parts = [f"{k} setups: {v['wins']}/{v['total']} ({v['win_rate']}% win)" for k, v in sorted(by_votes.items(), reverse=True)]
            insight = " · ".join(parts)

    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
    avg_win_pct = round(sum(t["pnl_pct"] for t in wins) / len(wins), 1) if wins else 0.0
    avg_loss_pct = round(sum(t["pnl_pct"] for t in losses) / len(losses), 1) if losses else 0.0  # negative
    # Expectancy: average % P&L per trade, blending win-rate with avg win/loss size.
    # Positive win rate alone can still be a net loser if losses are big relative to wins.
    expectancy_pct = round((win_rate / 100 * avg_win_pct) + ((1 - win_rate / 100) * avg_loss_pct), 2) if closed else None

    # Loss clustering — same bucket() helper, applied to the loss subset only,
    # to spot which vote-strength/instrument combos are actually losing money.
    loss_by_votes = bucket(losses, lambda t: t["votes"])
    loss_by_instrument = bucket(losses, lambda t: t["instrument"])

    MIN_SAMPLE = 10
    if len(closed) < MIN_SAMPLE:
        loss_diagnostic = f"Only {len(closed)} closed trades so far — need at least {MIN_SAMPLE} before a loss pattern means anything more than noise. Keep paper-trading."
    elif not losses:
        loss_diagnostic = f"No losses yet across {len(closed)} closed trades — too early to call this an edge, but nothing to fix either."
    else:
        # Find the vote-strength bucket with the worst loss concentration among buckets with >=2 losses.
        candidates = {k: v for k, v in loss_by_votes.items() if v["total"] >= 2}
        if candidates:
            worst_key = max(candidates.items(), key=lambda kv: kv[1]["total"] / max(by_votes.get(kv[0], {}).get("total", 1), 1))[0]
            worst = loss_by_votes[worst_key]
            total_at_that_vote = by_votes.get(worst_key, {}).get("total", worst["total"])
            other_votes = {k: v for k, v in by_votes.items() if k != worst_key}
            best_other = max(other_votes.items(), key=lambda kv: kv[1]["win_rate"], default=None)
            tail = f" {best_other[0]} setups are {best_other[1]['win_rate']}% win so far — consider raising your entry threshold." if best_other and best_other[1]["win_rate"] > by_votes.get(worst_key, {}).get("win_rate", 0) else ""
            loss_diagnostic = f"Your losses are concentrated in {worst_key}-vote setups ({worst['total']} of {len(losses)} losses, out of {total_at_that_vote} total {worst_key} trades).{tail}"
        else:
            loss_diagnostic = f"{len(losses)} losses so far, spread thinly across vote-strengths — no single pattern stands out yet."

    return {
        "total_closed": len(closed),
        "open_count": len(open_),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win_pct": avg_win_pct,
        "avg_loss_pct": avg_loss_pct,
        "expectancy_pct": expectancy_pct,
        "by_instrument": by_instrument,
        "by_votes": by_votes,
        "loss_by_votes": loss_by_votes,
        "loss_by_instrument": loss_by_instrument,
        "insight": insight,
        "loss_diagnostic": loss_diagnostic,
    }


def main():
    now_str = now_ist_str()
    if not SIGNAL_FILE.exists():
        print("No fo_latest.json found — run refresh_fo_cloud.py first. Skipping.")
        return

    signals = json.loads(SIGNAL_FILE.read_text(encoding="utf-8"))
    journal = _load_journal()
    trades = journal.get("trades", [])

    for inst_name in INSTRUMENTS:
        sig = signals.get(inst_name)
        if not sig or "error" in sig:
            continue

        open_trade = next((t for t in trades if t["instrument"] == inst_name and t["status"] == "open"), None)

        if open_trade:
            _mark_to_market(open_trade, sig, now_str)
            print(f"  {inst_name}: marked open trade {open_trade['id']} -> {open_trade['status']} ({open_trade.get('exit_reason', 'still open')})")
            # signal flip immediately opens a fresh trade in the new direction
            if open_trade["status"] == "closed" and open_trade["exit_reason"] == "signal_flip" and sig["consensus"] != "WAIT":
                new_trade = _open_trade(inst_name, sig, now_str)
                trades.append(new_trade)
                print(f"  {inst_name}: opened {new_trade['id']} (signal flip)")
        elif sig["consensus"] != "WAIT":
            new_trade = _open_trade(inst_name, sig, now_str)
            trades.append(new_trade)
            print(f"  {inst_name}: opened {new_trade['id']}")
        else:
            print(f"  {inst_name}: WAIT - no open trade, nothing to do")

    journal["trades"] = trades
    journal["stats"] = _compute_stats(trades)
    journal["_meta"] = {"generated_at": now_str}

    JOURNAL_FILE.write_text(json.dumps(journal, indent=2), encoding="utf-8")
    print(f"  Wrote {JOURNAL_FILE} — {journal['stats']['total_closed']} closed, {journal['stats']['open_count']} open, {journal['stats']['win_rate']}% win rate")
    print("Done.")


if __name__ == "__main__":
    main()
