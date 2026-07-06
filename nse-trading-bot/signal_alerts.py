"""
Actionable-only Telegram alerts — per the user's explicit request
(06-Jul-2026): "don't message me each refresh, just message me when we have
a strong buy or it's time to exit an open trade."

Runs at the end of every refresh, AFTER all the JSON feeds are written.
Compares the fresh signals against logs/alert_state.json (what was already
alerted) and sends ONE batched Telegram message covering only what is NEW
this run:

  BUY side:
    - Index F&O consensus flipping to BUY_CE/BUY_PE (fo_latest.json), with
      the full suggested trade (strike/premium/SL/targets)
    - A stock newly reaching STRONG_BUY in the equity scan (equity_scan.json)

  EXIT side:
    - A paper trade closing in trade_journal.json (target hit / stop loss /
      expiry / signal flip) — "if you mirrored this trade, act"
    - A REAL tracked equity position (equity_journal.json) crossing below
      its SL or above T1/T2

Dedup rules (the whole point — no repeats while a condition persists):
  - F&O: alert only when consensus CHANGES to an actionable value; state
    stores the last-seen consensus per instrument, so BUY_CE staying BUY_CE
    for 3 days = one alert on day 1, silence after.
  - Equity STRONG_BUY: alert symbols not in the previous strong-buy set. A
    symbol that drops out and later re-enters alerts again (that's a new
    event, not a repeat).
  - Paper trades: alert each closed trade id exactly once, forever.
  - Real positions: alert each (symbol, status) pair once — flapping around
    T1 doesn't re-ping, but escalating target1_hit -> target2_hit does.
    Symbols no longer tracked are pruned so a re-buy can alert fresh.

If there is nothing new, NOTHING is sent (not even a "no signals" ping).
Routine run-outcome pings were removed from record_run_outcome.py the same
day; CRITICAL failure alerts remain there. The healthchecks.io dead-man's
switch (in the workflow's gate job) still covers "pipeline went silent".

State file lives in logs/ next to run_history.json — internal CI state,
deliberately NOT a dashboard feed, so it is NOT in vercel.json or
validate_json_outputs.py's SCHEMA (that 3-place checklist is for
client-fetched feeds only).

Never crashes the chain: any unexpected error prints and exits 0.
Messages are composed ASCII-only ("Rs.", no emoji) — same cp1252 console
lesson as the rest of this codebase, and it keeps the printed copy of every
message identical to what Telegram receives.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist_str
from alerts import send_alert

REPO_ROOT = pathlib.Path(__file__).parent.parent
STATE_FILE = REPO_ROOT / "logs" / "alert_state.json"

FO_FILE = REPO_ROOT / "fo_latest.json"
SCAN_FILE = REPO_ROOT / "equity_scan.json"
PAPER_FILE = REPO_ROOT / "trade_journal.json"
EQUITY_FILE = REPO_ROOT / "equity_journal.json"
KITE_PORTFOLIO_FILE = REPO_ROOT / "kite_portfolio.json"

ALERTED_TRADE_IDS_MAX = 500
ACTIONABLE_POSITION_STATUSES = ("below_sl", "target1_hit", "target2_hit")


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_state():
    state = _load_json(STATE_FILE)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("fo_consensus", {})
    state.setdefault("strong_buys", [])
    state.setdefault("alerted_trade_ids", [])
    state.setdefault("position_statuses_alerted", {})
    return state


def _save_state(state):
    state["updated_at"] = now_ist_str()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _fo_buy_events(fo, state):
    """Consensus transitions into BUY_CE/BUY_PE. State updated to the
    current consensus either way, so a flip away and back re-alerts."""
    events = []
    if not isinstance(fo, dict):
        return events
    for inst, sig in fo.items():
        if inst == "_meta" or not isinstance(sig, dict) or "error" in sig:
            continue
        cur = sig.get("consensus")
        prev = state["fo_consensus"].get(inst)
        if cur in ("BUY_CE", "BUY_PE") and cur != prev:
            t = sig.get("trade") or {}
            detail = ""
            if t:
                detail = (f" -> {t.get('action', '')} ~Rs.{t.get('entry_premium', '?')}"
                          f" | SL {t.get('sl_premium', '?')} | T1 {t.get('target1_premium', '?')}"
                          f" | T2 {t.get('target_premium', '?')} | exp {t.get('expiry', '?')}")
            votes = sig.get("ce_votes") if cur == "BUY_CE" else sig.get("pe_votes")
            events.append(f"- {inst}: {cur} ({votes} votes){detail}")
        state["fo_consensus"][inst] = cur
    return events


def _equity_strong_buy_events(scan, state):
    events = []
    if not isinstance(scan, dict):
        return events
    cur = {}
    for sym, d in scan.items():
        if sym == "_meta" or not isinstance(d, dict):
            continue
        if d.get("signal") == "STRONG_BUY":
            cur[sym] = d
    prev = set(state["strong_buys"])
    for sym in sorted(cur):
        if sym not in prev:
            d = cur[sym]
            events.append(f"- {sym}: STRONG_BUY ({d.get('buy_votes', '?')}/4 voters)"
                          f" @ Rs.{d.get('entry', '?')} | SL {d.get('sl', '?')}"
                          f" | T1 {d.get('t1', '?')} | T2 {d.get('t2', '?')}")
    state["strong_buys"] = sorted(cur)
    return events


def _paper_exit_events(journal, state):
    events = []
    trades = (journal or {}).get("trades")
    if not isinstance(trades, list):
        return events
    alerted = set(state["alerted_trade_ids"])
    for t in trades:
        tid = t.get("id")
        if not tid or t.get("status") != "closed" or tid in alerted:
            continue
        reason = (t.get("exit_reason") or "?").upper()
        pnl = t.get("pnl_pct")
        pnl_str = f"{pnl:+.1f}%" if isinstance(pnl, (int, float)) else "?"
        events.append(f"- {t.get('instrument', '?')} {t.get('strike', '?')} {t.get('option_type', '?')}"
                      f" (paper): {reason} @ Rs.{t.get('exit_premium', '?')} ({pnl_str})"
                      f" -- act if you mirrored this trade")
        alerted.add(tid)
    state["alerted_trade_ids"] = list(alerted)[-ALERTED_TRADE_IDS_MAX:]
    return events


def _kite_fo_watch_events(kite, fo, state):
    """'Keep close watch' on the user's REAL held F&O positions (from
    kite_portfolio.json's positions[]): alert when the underlying index
    signal stops backing the direction they hold — i.e. holding a CE while
    consensus flips to WAIT or BUY_PE. That's the system's own thesis
    reversing on a live real-money position: a "time to exit" event.

    This is the only F&O-exit signal we can raise server-side: the user's
    own SL/T1/T2 premium targets live in browser localStorage (foPositions),
    never synced here, so an exact "hit your stop" alert isn't possible from
    CI yet. Signal-flip is the honest, data-backed proxy.

    Dedup: state['fo_position_signal'][symbol] stores the last consensus
    seen for that contract, so a persistent non-supportive signal alerts
    once, not every run; a flip away and back re-alerts (a genuinely new
    event). Symbols no longer held are pruned so a re-entry alerts fresh.
    Post-market Kite sometimes returns an empty positions[] even with a live
    session (seen 06-Jul-2026) — that just yields no events this run, and
    the prune step is skipped so state survives the blip rather than
    forgetting a still-open position."""
    events = []
    positions = (kite or {}).get("positions")
    if not isinstance(positions, list) or not isinstance(fo, dict):
        return events
    watched = state.setdefault("fo_position_signal", {})
    seen = set()
    for p in positions:
        sym = p.get("tradingsymbol", "")
        key = "BANKNIFTY" if sym.startswith("BANKNIFTY") else "NIFTY50" if sym.startswith("NIFTY") else None
        if not key:
            continue
        sig = fo.get(key)
        if not isinstance(sig, dict) or "error" in sig:
            continue
        seen.add(sym)
        held_dir = "BUY_CE" if sym.endswith("CE") else "BUY_PE"
        consensus = sig.get("consensus")
        supportive = (consensus == held_dir)
        prev = watched.get(sym)
        if not supportive and prev != consensus:
            reason = "is now neutral (WAIT)" if consensus == "WAIT" else f"flipped to {consensus}"
            events.append(f"- {sym} (your live position): underlying signal {reason} — no longer backing your "
                          f"{sym[-2:]}. The system's thesis has reversed here; review your exit.")
        watched[sym] = consensus
    # Only prune when Kite actually returned positions -- an empty post-market
    # response shouldn't wipe state for positions the user still holds.
    if positions:
        state["fo_position_signal"] = {k: v for k, v in watched.items() if k in seen}
    return events


def _real_position_events(equity, state):
    events = []
    positions = (equity or {}).get("positions")
    if not isinstance(positions, list):
        return events
    alerted = state["position_statuses_alerted"]
    tracked_names = set()
    for p in positions:
        name = p.get("name")
        if not name:
            continue
        tracked_names.add(name)
        status = p.get("status")
        if status not in ACTIONABLE_POSITION_STATUSES:
            continue
        already = alerted.get(name, [])
        if status in already:
            continue
        label = {"below_sl": "BELOW STOP-LOSS -- exit per plan",
                 "target1_hit": "T1 HIT -- consider partial booking",
                 "target2_hit": "T2 HIT -- consider booking"}[status]
        events.append(f"- {name} (your position): {label}"
                      f" | live Rs.{p.get('current_price', '?')} vs SL {p.get('sl', '?')}"
                      f" / T1 {p.get('t1', '?')} / T2 {p.get('t2', '?')}"
                      f" ({p.get('pnl_pct', '?')}% since entry)")
        alerted[name] = already + [status]
    # prune symbols no longer tracked so a future re-buy alerts fresh
    state["position_statuses_alerted"] = {k: v for k, v in alerted.items() if k in tracked_names}
    return events


def main():
    state = _load_state()

    fo = _load_json(FO_FILE)
    buy_events = (_fo_buy_events(fo, state)
                  + _equity_strong_buy_events(_load_json(SCAN_FILE), state))
    exit_events = (_paper_exit_events(_load_json(PAPER_FILE), state)
                   + _real_position_events(_load_json(EQUITY_FILE), state)
                   + _kite_fo_watch_events(_load_json(KITE_PORTFOLIO_FILE), fo, state))

    _save_state(state)

    if not buy_events and not exit_events:
        print("  No new buy/exit signals this run -- nothing sent (by design).")
        return

    parts = [f"Signals at {now_ist_str()}"]
    if buy_events:
        parts.append("\nNEW BUY SIGNALS:\n" + "\n".join(buy_events))
    if exit_events:
        parts.append("\nEXIT SIGNALS:\n" + "\n".join(exit_events))
    parts.append("\nEducational signals (EOD-based) -- verify in your broker before acting.")
    message = "\n".join(parts)

    print("  Composed alert:")
    for line in message.splitlines():
        print("  | " + line)
    send_alert(message, level="SIGNAL")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never break the refresh chain over an alerting problem.
        print(f"  ERROR in signal_alerts main(): {e} -- continuing without alert")
