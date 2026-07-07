"""
Daily Trading Brain progress report -> one Telegram message per day.

User asked (07-Jul-2026) for a DAILY report on Trading Brain progress. This
is deliberately ONCE A DAY (fired on the 9pm IST prep run), NOT a per-refresh
ping -- fully consistent with the standing "no routine per-run pings" rule
(see signal_alerts.py); the user is explicitly opting into this one daily
digest.

Shows PROGRESS, not just a static snapshot: it stores the previous report's
key metrics in logs/daily_report_state.json and reports the day-over-day
delta (win rate change, trades closed since yesterday), plus what actually
happened today (trades closed today, new recommendations opened today).

Covers three layers, Trading Brain first (that's what the user named):
  1. trade_journal.json   -- F&O paper-trade win/loss/expectancy (the Brain)
  2. recommendation_journal.json -- every signal scored (broader progress)
  3. strategy_performance.json   -- the forward scoreboard (if decisive recs)

Idempotent per day: only sends if it hasn't already sent today (dedup on
logs/daily_report_state.json's date), so a manual workflow_dispatch after
the scheduled 9pm run won't double-send.

Anti-hallucination: real numbers only from the real journals; empty/missing
data -> honest "no data yet", never fabricated. now_ist_str(), utf-8 writes,
NO emoji in source/print (cp1252) -- Telegram renders the plain text fine.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist, now_ist_str
from alerts import send_alert

REPO_ROOT = pathlib.Path(__file__).parent.parent
TRADE_JOURNAL = REPO_ROOT / "trade_journal.json"
REC_JOURNAL = REPO_ROOT / "recommendation_journal.json"
STRATEGY_PERF = REPO_ROOT / "strategy_performance.json"
MARKET_REGIME = REPO_ROOT / "market_regime.json"
MARKET_MOOD = REPO_ROOT / "market_mood.json"
SECTOR_ROTATION = REPO_ROOT / "sector_rotation.json"
EXPERT_GATE = REPO_ROOT / "expert_gate.json"
EQUITY_SCAN = REPO_ROOT / "equity_scan.json"
STATE_FILE = REPO_ROOT / "logs" / "daily_report_state.json"


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _today_str():
    return now_ist().strftime("%d %b %Y")


def _is_today(ts):
    """ts like '04 Jul 2026 11:25 IST' -> True if its date is today (IST)."""
    return bool(ts) and ts.startswith(_today_str())


def _fmt_delta(cur, prev, suffix="", plus_is_good=True):
    if prev is None or cur is None:
        return ""
    d = round(cur - prev, 1)
    if d == 0:
        return " (no change)"
    sign = "+" if d > 0 else ""
    return f" ({sign}{d}{suffix} since yesterday)"


def _trading_brain_section(tj, state):
    s = (tj or {}).get("stats") or {}
    trades = (tj or {}).get("trades") or []
    total_closed = s.get("total_closed", 0)
    win_rate = s.get("win_rate")
    open_count = s.get("open_count", 0)
    expectancy = s.get("expectancy_pct")

    closed_today = [t for t in trades if t.get("status") == "closed" and _is_today(t.get("closed_at"))]
    lines = ["PAPER TRADING BRAIN (F&O)"]
    wr_txt = f"{win_rate}%" if win_rate is not None else "n/a"
    lines.append(f"- Win rate: {wr_txt} ({s.get('wins', 0)}W / {s.get('losses', 0)}L, {total_closed} closed)"
                 + _fmt_delta(win_rate, state.get("last_win_rate"), "pp"))
    lines.append(f"- Open paper trades: {open_count}")
    if expectancy is not None:
        lines.append(f"- Expectancy: {'+' if expectancy > 0 else ''}{expectancy}% per trade")
    closed_delta = total_closed - (state.get("last_total_closed") or 0)
    if closed_today:
        for t in closed_today:
            pnl = t.get("pnl_pct")
            pnl_txt = f"{'+' if (pnl or 0) > 0 else ''}{pnl}%" if pnl is not None else "?"
            lines.append(f"- Closed today: {t.get('instrument')} {t.get('strike')} {t.get('option_type')} "
                         f"{t.get('exit_reason', '?')} {pnl_txt}")
    elif closed_delta > 0:
        lines.append(f"- {closed_delta} trade(s) closed since the last report")
    else:
        lines.append("- No paper trades closed today")
    return "\n".join(lines), {"win_rate": win_rate, "total_closed": total_closed}


def _rec_journal_section(rj):
    recs = (rj or {}).get("recommendations") or []
    sm = (rj or {}).get("summary") or {}
    opened_today = sum(1 for r in recs if _is_today(r.get("opened_at")))
    closed_today = sum(1 for r in recs if r.get("status") in ("won", "lost", "expired", "invalidated") and _is_today(r.get("closed_at")))
    lines = ["RECOMMENDATION JOURNAL (every signal scored)"]
    dec = sm.get("decisive_count", 0)
    wr = sm.get("win_rate_pct")
    lines.append(f"- {sm.get('open_count', 0)} open, {dec} decisive"
                 + (f", {wr}% win" if wr is not None else " (accumulating)"))
    lines.append(f"- Today: {opened_today} new signal(s) opened, {closed_today} closed/resolved")
    return "\n".join(lines)


def _strategy_perf_section(sp):
    o = (sp or {}).get("overall") or {}
    lines = ["STRATEGY PERFORMANCE"]
    if not o.get("decisive"):
        lines.append("- Not enough decisive recs yet -- still accumulating (no reliable stats before ~20)")
    else:
        lines.append(f"- {o.get('win_rate_pct')}% win over {o.get('decisive')} decisive, "
                     f"expectancy {'+' if (o.get('expectancy_pct') or 0) > 0 else ''}{o.get('expectancy_pct')}%, "
                     f"PF {o.get('profit_factor')}, maxDD {o.get('max_drawdown_pct')}%")
        if (sp or {}).get("sample_size_warning"):
            lines.append("- (small sample -- treat as directional, not conclusive)")
    return "\n".join(lines)


def _next_session_outlook(regime, mood, sectors, gate, equity):
    """Honest, rule-based next-session outlook -- NOT a fabricated price
    forecast. Everything here is derived from today's real computed data:
    the regime classification, the ATR-based typical daily range (a
    statistical spread, NOT a direction call), the expert-gate posture, and
    the mood/sector context. This is what the system already knows, projected
    one session forward -- the disciplined 'set up for tomorrow' read, with no
    invented numbers."""
    insts = (regime or {}).get("instruments", {})
    rec = (regime or {}).get("recommendation", {})
    gate_insts = (gate or {}).get("instruments", {})
    lines = ["NEXT SESSION OUTLOOK (rule-based from today's close -- NOT a price forecast)"]

    for sym in ("NIFTY50", "BANKNIFTY"):
        v = insts.get(sym)
        if not isinstance(v, dict):
            continue
        spot = v.get("spot")
        atr_pct = v.get("atr_pct")
        rng = round(spot * atr_pct / 100) if (spot and atr_pct) else None
        g = gate_insts.get(sym) or {}
        gate_txt = g.get("state", "?")
        if g.get("direction"):
            gate_txt += " " + g["direction"]
        rng_txt = (f", typical 1-day range +/-~{rng} ({atr_pct}% ATR)" if rng else "")
        lines.append(f"- {sym}: {v.get('trend', '?')} (ADX {v.get('adx', '?')}). "
                     f"Close ~{round(spot):,}{rng_txt}. Gate: {gate_txt}.")

    best = rec.get("best_fit_strategies") or []
    avoid = rec.get("avoid") or []
    if best or avoid:
        lines.append(f"- Strategy fit: favor {', '.join(best) if best else 'none flagged'}"
                     + (f"; avoid {', '.join(avoid)}" if avoid else "; nothing to avoid"))
    reasoning = rec.get("reasoning") or []
    if reasoning:
        lines.append("  Why: " + reasoning[0])

    mood_txt = ""
    if isinstance(mood, dict) and mood.get("composite_score") is not None:
        mood_txt = f"Mood {mood['composite_score']} ({mood.get('label', '?')})"
    top = (sectors or {}).get("top_sectors") or []
    sec_txt = ("Momentum sectors: " + ", ".join(top[:3])) if top else ""
    sb = (((equity or {}).get("_meta") or {}).get("counts") or {}).get("strong_buy")
    ctx = " · ".join([p for p in [mood_txt, sec_txt, (f"{sb} equity strong-buy(s)" if sb else "")] if p])
    if ctx:
        lines.append("- Context: " + ctx)

    # Rule-based plan line (synthesis of gate + regime, not a prediction)
    states = [((gate_insts.get(s) or {}).get("state")) for s in ("NIFTY50", "BANKNIFTY")]
    choppy = any((insts.get(s) or {}).get("trend") == "Sideways/Choppy" for s in ("NIFTY50", "BANKNIFTY"))
    if any(st in ("CONFIRMED_ENTRY", "IN_TRADE") for st in states):
        plan = "An index setup is confirmed/held by the gate -- act on the confirmed side, respect the stop."
    elif any(st in ("EXIT_WATCH", "EXIT_CONFIRMED") for st in states):
        plan = "Gate is in exit-watch on a held setup -- be ready to exit, don't add."
    elif any(st == "SETUP_FORMING" for st in states):
        plan = ("Signals are forming but not gate-confirmed" + (" (choppy regime holding them back)" if choppy else "") +
                " -- wait for confirmation, don't chase the raw CE/PE flip.")
    else:
        plan = "No index setup right now -- stay patient; watch the momentum sectors for equity rotation."
    lines.append("- Plan: " + plan)
    return "\n".join(lines)


def main():
    state = _load(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    today = _today_str()
    if state.get("last_report_date") == today:
        print(f"  daily report already sent for {today} -- skipping (idempotent)")
        return

    tj = _load(TRADE_JOURNAL, {})
    rj = _load(REC_JOURNAL, {})
    sp = _load(STRATEGY_PERF, {})
    regime = _load(MARKET_REGIME, {})
    mood = _load(MARKET_MOOD, {})
    sectors = _load(SECTOR_ROTATION, {})
    gate = _load(EXPERT_GATE, {})
    equity = _load(EQUITY_SCAN, {})

    brain_txt, brain_metrics = _trading_brain_section(tj, state)
    message = (
        f"Daily Trading Brain Report -- {today}\n\n"
        + brain_txt + "\n\n"
        + _rec_journal_section(rj) + "\n\n"
        + _strategy_perf_section(sp) + "\n\n"
        + _next_session_outlook(regime, mood, sectors, gate, equity) + "\n\n"
        + "Educational paper/virtual track record + rule-based outlook, not advice or a price forecast. Verify in Kite before acting."
    )

    print("  Composed daily report:")
    for line in message.splitlines():
        print("  | " + line)

    sent = send_alert(message, level="INFO")

    # Persist today's metrics for tomorrow's delta -- store the date regardless
    # of send success so a Telegram outage doesn't cause a double-send tomorrow;
    # but only mark "reported today" if it actually went out, so a failed send
    # is retried on the next run today rather than silently skipped.
    new_state = {
        "last_win_rate": brain_metrics["win_rate"],
        "last_total_closed": brain_metrics["total_closed"],
        "last_run_at": now_ist_str(),
    }
    if sent:
        new_state["last_report_date"] = today
    else:
        new_state["last_report_date"] = state.get("last_report_date")  # unchanged -> retry later today
        print("  (Telegram send did not succeed -- will retry on the next run today)")
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(new_state, indent=2), encoding="utf-8")
    print(f"  Wrote {STATE_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  ERROR in daily_report main(): {e} -- no report sent")
