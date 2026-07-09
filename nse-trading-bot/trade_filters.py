"""
Module 2 (Master Brief Part 3): trade_filters.py — the judgment layer that
runs BEFORE any order would be sized or placed. Filters 1-4 only, per the
brief's own build order ("Module 2 filters 1-4, pause, then 5-6") — 5
(correlation/exposure) and 6 (liquidity/spread) are not built yet.

There is no executor to call this yet (kite_executor.py is blocked on the
SEBI static-whitelisted-IP requirement — see CLAUDE.md) so this module is
deliberately self-contained and independently testable: pass it a trade
candidate + whatever context you have and it tells you pass/fail per filter,
with a reason string, logged to logs/filter_log.json. Wire it into whichever
script actually proposes a trade once a real entry point exists (today,
trade_brain.py's _open_trade() is the closest analogue).

Filter 1 — market regime: block when the candidate's strategy is in today's
  regime "avoid" list (market_regime.json's own recommendation, computed in
  market_regime_core.py from NIFTY/BANKNIFTY EMA+ADX). Fails CLOSED (blocks)
  if regime data is unavailable — matches the Brief's general fail-safe
  posture ("unknown state = do nothing"), not fail-open.
Filter 2 — time window: blocks the first/last N minutes of the session
  (spread/volatility risk) and an optional lunch window.
Filter 3 — loss streak breaker: N consecutive losses (most recent first) =
  no new entries until reset.
Filter 4 — R:R rejection: reject before sizing if reward:risk is below the
  product's configured minimum.

All thresholds live in config.py, nothing hardcoded here. Every filter is
independently toggleable via config.FILTERS_ENABLED.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, time as dtime

from ist_time import now_ist, now_ist_str
from alerts import send_alert
import config

REPO_ROOT = pathlib.Path(__file__).parent.parent
MARKET_REGIME_FILE = REPO_ROOT / config.MARKET_REGIME_FILE
TRADE_JOURNAL_FILE = REPO_ROOT / "trade_journal.json"
FILTER_LOG_FILE = REPO_ROOT / "logs" / "filter_log.json"
FILTER_LOG_MAX = 200


def _result(name, passed, reason):
    return {"filter": name, "passed": passed, "reason": reason}


def _parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _minutes(t):
    return t.hour * 60 + t.minute


def _add_minutes(t, n):
    total = _minutes(t) + n
    return dtime(total // 60 % 24, total % 60)


def _subtract_minutes(t, n):
    total = _minutes(t) - n
    return dtime(total // 60 % 24, total % 60)


# --- Filter 1: market regime ---------------------------------------------

def load_market_regime():
    if not MARKET_REGIME_FILE.exists():
        return None
    try:
        return json.loads(MARKET_REGIME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def filter_market_regime(candidate, regime=None):
    if not config.FILTERS_ENABLED["market_regime"]:
        return _result("market_regime", True, "disabled in config")
    if regime is None:
        regime = load_market_regime()
    if regime is None:
        return _result("market_regime", False, "market_regime.json unavailable — fail closed, not fail open")

    avoid = regime.get("recommendation", {}).get("avoid", [])
    strategy = candidate.get("strategy")
    if strategy and strategy in avoid:
        reasoning = "; ".join(regime.get("recommendation", {}).get("reasoning", [])) or "see market_regime.json"
        return _result("market_regime", False, f"{strategy} is in today's regime avoid-list — {reasoning}")
    return _result("market_regime", True, f"{strategy or 'candidate'} not flagged for the current regime")


# --- Filter 2: time window -------------------------------------------------

def filter_time_window(now_dt=None):
    if not config.FILTERS_ENABLED["time_window"]:
        return _result("time_window", True, "disabled in config")
    now_dt = now_dt or now_ist()
    t = now_dt.time()

    open_t = _parse_hhmm(config.SESSION_OPEN)
    close_t = _parse_hhmm(config.SESSION_CLOSE)
    first_block_end = _add_minutes(open_t, config.BLOCK_FIRST_MINUTES_OF_SESSION)
    last_block_start = _subtract_minutes(close_t, config.BLOCK_LAST_MINUTES_OF_SESSION)

    if open_t <= t < first_block_end:
        return _result("time_window", False, f"within first {config.BLOCK_FIRST_MINUTES_OF_SESSION} min of session ({config.SESSION_OPEN}-{first_block_end.strftime('%H:%M')}) — opening volatility/spread risk")
    if last_block_start <= t <= close_t:
        return _result("time_window", False, f"within last {config.BLOCK_LAST_MINUTES_OF_SESSION} min of session ({last_block_start.strftime('%H:%M')}-{config.SESSION_CLOSE}) — closing volatility/pin risk")
    if config.BLOCK_LUNCH_WINDOW:
        lunch_start = _parse_hhmm(config.LUNCH_WINDOW_START)
        lunch_end = _parse_hhmm(config.LUNCH_WINDOW_END)
        if lunch_start <= t < lunch_end:
            return _result("time_window", False, f"within lunch block ({config.LUNCH_WINDOW_START}-{config.LUNCH_WINDOW_END}) — low liquidity")
    return _result("time_window", True, "outside all blocked windows")


# --- Filter 3: loss streak breaker -----------------------------------------

def _recent_results_from_trade_journal(limit=10):
    """Convenience loader — most-recent-first list of {"result": "win"/"loss"}
    from trade_journal.json's closed F&O paper trades. Not the only valid
    source (equity's closedTrades in my_positions.json is another) — callers
    can build their own recent_trades list and pass it in directly instead.

    Returns [] when the file genuinely doesn't exist yet (no history = safe
    to pass), but None when it exists and failed to read/parse — those are
    NOT the same thing. filter_loss_streak must fail CLOSED on None, not
    silently treat a corrupt journal as "no losses ever" (this used to
    collapse both cases to [], which fails open on data corruption — not a
    safety filter at that point)."""
    if not TRADE_JOURNAL_FILE.exists():
        return []
    try:
        journal = json.loads(TRADE_JOURNAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    closed = [t for t in journal.get("trades", []) if t.get("status") == "closed" and t.get("result")]
    closed.sort(key=lambda t: t.get("closed_at") or "", reverse=True)
    return closed[:limit]


def filter_loss_streak(recent_trades):
    if not config.FILTERS_ENABLED["loss_streak"]:
        return _result("loss_streak", True, "disabled in config")
    if recent_trades is None:
        return _result("loss_streak", False, "trade_journal.json unavailable/corrupt — fail closed, not fail open")
    if not recent_trades:
        return _result("loss_streak", True, "no trade history available yet")

    streak = 0
    for t in recent_trades:  # assumed most-recent-first
        if t.get("result") == "loss":
            streak += 1
        else:
            break

    if streak >= config.LOSS_STREAK_LIMIT:
        return _result("loss_streak", False, f"{streak} consecutive losses (limit {config.LOSS_STREAK_LIMIT}) — no new entries until reset")
    return _result("loss_streak", True, f"{streak} consecutive loss(es), below the {config.LOSS_STREAK_LIMIT} limit")


# --- Filter 4: R:R rejection ------------------------------------------------

def filter_risk_reward(candidate):
    if not config.FILTERS_ENABLED["risk_reward"]:
        return _result("risk_reward", True, "disabled in config")

    entry, stop, target = candidate.get("entry"), candidate.get("stop"), candidate.get("target")
    product = (candidate.get("product") or "MIS").upper()
    direction = (candidate.get("direction") or "BUY").upper()
    if entry is None or stop is None or target is None:
        return _result("risk_reward", False, "missing entry/stop/target — cannot evaluate R:R, fail closed")

    # abs()-based risk/reward below is direction-agnostic by construction, which
    # means on its own it can't catch a candidate with stop/target on the wrong
    # side of entry for the stated direction (e.g. a BUY with its stop ABOVE
    # entry) -- it would still compute a "passing" ratio for a nonsensical
    # trade. Validate geometry explicitly, per direction, before trusting the
    # ratio.
    if direction == "BUY":
        if not (stop < entry < target):
            return _result("risk_reward", False, f"BUY candidate geometry invalid (stop={stop}, entry={entry}, target={target}) — expected stop < entry < target, fail closed")
    elif direction == "SELL":
        if not (target < entry < stop):
            return _result("risk_reward", False, f"SELL candidate geometry invalid (stop={stop}, entry={entry}, target={target}) — expected target < entry < stop, fail closed")
    else:
        return _result("risk_reward", False, f"unknown direction '{direction}' — fail closed")

    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return _result("risk_reward", False, "zero stop distance — R:R undefined, reject (also flags a sizing bug upstream)")

    rr = round(reward / risk, 2)
    min_rr = config.MIN_RR_CNC if product == "CNC" else config.MIN_RR_MIS
    if rr < min_rr:
        return _result("risk_reward", False, f"RR_TOO_LOW: {rr} < required {min_rr} for {product}")
    return _result("risk_reward", True, f"R:R {rr} meets the {min_rr} minimum for {product}")


# --- Combined evaluation ----------------------------------------------------

def _append_filter_log(entry):
    FILTER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        log = json.loads(FILTER_LOG_FILE.read_text(encoding="utf-8")) if FILTER_LOG_FILE.exists() else []
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []
    log.append(entry)
    log = log[-FILTER_LOG_MAX:]
    FILTER_LOG_FILE.write_text(json.dumps(log, indent=2), encoding="utf-8")


def _run_filters(candidate, regime=None, recent_trades=None, now_dt=None):
    """Pure -- no logging, no alerting, just the 4 filter results. Extracted
    from evaluate_trade() 09-Jul-2026 so a per-refresh scorer (context_score.py)
    can call the filters for informational scoring across many candidates
    without firing a Telegram WARNING and a filter_log.json entry for every
    one -- evaluate_trade() (below) is unchanged for its one existing use
    case (an eventual real executor) and keeps 100% of its current logging/
    alerting behavior; this is purely an extraction, verified via repo-wide
    grep to have zero existing callers of evaluate_trade() to break."""
    return [
        filter_market_regime(candidate, regime),
        filter_time_window(now_dt),
        filter_loss_streak(recent_trades if recent_trades is not None else _recent_results_from_trade_journal()),
        filter_risk_reward(candidate),
    ]


def evaluate_trade(candidate, regime=None, recent_trades=None, now_dt=None):
    """candidate: {"symbol", "strategy", "product" ("MIS"/"CNC"), "direction"
    ("BUY"/"SELL", defaults to "BUY" if omitted), "entry", "stop", "target"}.
    Returns {"passed": bool, "results": [...]} and logs every evaluation
    (pass or block) to logs/filter_log.json, alerting on WARNING for any
    block per the Brief's alert-level spec."""
    results = _run_filters(candidate, regime=regime, recent_trades=recent_trades, now_dt=now_dt)
    blocked = [r for r in results if not r["passed"]]

    verdict = {
        "timestamp": now_ist_str(),
        "candidate": candidate,
        "passed": not blocked,
        "results": results,
    }
    _append_filter_log(verdict)

    if blocked:
        reasons = "; ".join(f"{r['filter']}: {r['reason']}" for r in blocked)
        send_alert(f"Trade blocked ({candidate.get('symbol', '?')} / {candidate.get('strategy', '?')}): {reasons}", level="WARNING")

    return verdict


if __name__ == "__main__":
    # Self-test / demo — no executor exists yet to call this for real, so
    # this is how to sanity-check filter behavior locally per the Brief's
    # "filters testable individually" requirement.
    demo_candidate = {
        "symbol": "BANKNIFTY",
        "strategy": "Cianni",
        "product": "MIS",
        "entry": 100.0,
        "stop": 90.0,
        "target": 108.0,  # R:R = 0.8 -> should fail filter 4
    }
    verdict = evaluate_trade(demo_candidate, recent_trades=[{"result": "loss"}, {"result": "loss"}, {"result": "loss"}])
    print(json.dumps(verdict, indent=2))
