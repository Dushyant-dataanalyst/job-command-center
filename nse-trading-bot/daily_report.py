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

    brain_txt, brain_metrics = _trading_brain_section(tj, state)
    message = (
        f"Daily Trading Brain Report -- {today}\n\n"
        + brain_txt + "\n\n"
        + _rec_journal_section(rj) + "\n\n"
        + _strategy_perf_section(sp) + "\n\n"
        + "Educational paper/virtual track record, not advice. Verify in Kite before acting."
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
