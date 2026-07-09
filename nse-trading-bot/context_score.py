"""
Context Score — the "no raw signal becomes a decision on its own" layer
(learning-engine roadmap item 5). Of the 5-module architecture the user
specified, 4 already existed before this file was written: macro_risk_
refresh.py, recommendation_tracker.py, expert_gate.py, strategy_performance.py.
This module builds NOTHING NEW underneath — it reads what those 4 modules
(plus market_regime_core.py and trade_filters.py) already computed and
combines it into one unified per-instrument read: a 0-100 context_score and
a state from a common vocabulary (NO_TRADE/WATCH/SETUP_FORMING/
CONFIRMED_ENTRY/HOLD/EXIT_WATCH/EXIT_CONFIRMED/COOLDOWN) instead of a raw
BUY_CE/BUY_PE/BUY/STRONG_BUY.

READ-ONLY, INFORMATIONAL ONLY for fo_stock and equity — explicit user
decision, not an oversight: gating.hard_blocked is hardcoded False for both
kinds. This module can never suppress a stock F&O or equity signal, only
label it. Only fo_index may show hard_blocked=True, and even then it is a
READ-THROUGH of expert_gate.py's / macro_gate.py's OWN existing block —
never a new gate invented here.

ZERO-DRIFT IMPORTS: expert_gate.json's state is read, not recomputed;
trade_filters._run_filters() is imported and called, not copy-pasted;
recommendation_tracker's _flip_is_against()/_open_key() are imported and
reused for the exact same flip-detection / open-position-matching logic the
journal itself uses.

ANTI-FABRICATION: each of the 6 score components (macro/regime/sector/
instrument_quality/trade_quality/learning_history) is None when its input is
unavailable this run — never a fabricated neutral/default score.
context_score is a weighted average RENORMALIZED across only the components
actually available, with inputs_used recording exactly what fed it.

PHASE HISTORY: Phase A (09-Jul-2026) shipped macro/regime/expert-gate-read-
through/instrument_quality/trade_quality/learning_history, with sector
always None. Phase B (same day) added strategy_performance.json's
by_macro_risk_level slice — learning_history reads it defensively via
.get() so no change was needed here. Phase C (same day) wired the sector
component to sector_rotation.json's new rs_vs_nifty_pct field via
equity_scan_core._ticker_sector_map() — still None for fo_index (no sector
concept) and for stock-F&O tickers that don't resolve to any of the 10
sectors (LT/BAJFINANCE/TITAN/BHARTIARTL).

NOT WIRED INTO CI as of this commit. See context_score_dryrun.py for the
manual-review verification step required before this joins the live
pipeline — same "audit -> approval -> validate first -> wire in" order this
project already uses for backtest.py / trade_filters.py.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

import pandas as pd

from ist_time import now_ist, now_ist_str
from macro_gate import load_macro_risk, direction_blocked, macro_context
from trade_filters import _run_filters
from recommendation_tracker import _flip_is_against, _open_key
from equity_scan_core import _ticker_sector_map
from market_data import get_ohlcv

REPO_ROOT = pathlib.Path(__file__).parent.parent
FO_FILE = REPO_ROOT / "fo_latest.json"
STOCK_FO_FILE = REPO_ROOT / "stock_fo.json"
EQUITY_FILE = REPO_ROOT / "equity_scan.json"
REGIME_FILE = REPO_ROOT / "market_regime.json"
SECTOR_ROTATION_FILE = REPO_ROOT / "sector_rotation.json"
EXPERT_GATE_FILE = REPO_ROOT / "expert_gate.json"
STRATEGY_PERF_FILE = REPO_ROOT / "strategy_performance.json"
JOURNAL_FILE = REPO_ROOT / "recommendation_journal.json"
OUT_FILE = REPO_ROOT / "context_score.json"

FO_INDEX_INSTRUMENTS = ("NIFTY50", "BANKNIFTY")  # same set expert_gate.py gates
TREND_ADX_THRESHOLD = 20  # same constant market_regime_core.py itself uses

# fo_stock/equity score-bucket thresholds (0-100) -- tunable, named per this
# project's "nothing magic buried in logic" convention (see expert_gate.py)
SCORE_NO_TRADE_BELOW = 35
SCORE_CONFIRMED_ENTRY_AT_OR_ABOVE = 65
COOLDOWN_LOOKBACK_DAYS = 3            # calendar-day approximation of expert_gate's refresh-counted cooldown
MIN_DECISIVE_FOR_LEARNING_HISTORY = 20  # same floor strategy_performance.py itself applies

# Earnings/news-shock detection (Phase D, added 09-Jul-2026) -- a per-symbol
# caution flag, fo_stock/equity only (no "earnings" concept for an index).
# NOT tied to any real earnings calendar (no such data source exists
# anywhere in this project) -- purely a statistical read off OHLCV data
# already flowing through market_data.get_ohlcv(), same rel_volume/ATR-style
# pattern equity_scan_core.py/market_regime_core.py already use elsewhere.
EARNINGS_SHOCK_LOOKBACK_DAYS = 3
EARNINGS_SHOCK_MIN_ABS_RETURN_PCT = 4.0
EARNINGS_SHOCK_MIN_REL_VOLUME = 2.0
EARNINGS_SHOCK_SCORE_PENALTY = 15  # shaved off instrument_quality's score, never a hard block

STATE_DISPLAY_MAP = {"IN_TRADE": "HOLD"}  # display-only rename; never touches expert_gate.json itself

# Component weights, RENORMALIZED per-run across whichever components are
# actually available this refresh (see _weighted_score) -- these are the
# full-data weights, not floors or caps.
COMPONENT_WEIGHTS = {
    "macro": 0.20,
    "regime": 0.20,
    "sector": 0.10,
    "instrument_quality": 0.20,
    "trade_quality": 0.15,
    "learning_history": 0.15,
}


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_ist_date(ts):
    try:
        return datetime.strptime(ts.replace(" IST", ""), "%d %b %Y %H:%M").date()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Components -- each returns {"score": 0-100, "detail": "..."} or None.
# direction here is normalized to "long"/"short" (not the raw BUY_CE/BUY_PE/
# BUY/STRONG_BUY strings) so macro/regime alignment logic is shared across
# all 3 kinds instead of three near-duplicate branches.
# ---------------------------------------------------------------------------

def _macro_component(macro, direction):
    if macro is None or direction is None:
        return None
    bias = macro.get("bias")
    risk_level = macro.get("risk_level")
    risk_score = macro.get("risk_score")
    if risk_score is None:
        return None
    base = max(0, 100 - risk_score)  # less systemic risk = higher baseline score
    aligned = (direction == "long" and bias == "BULLISH") or (direction == "short" and bias == "BEARISH")
    opposed = (direction == "long" and bias == "BEARISH") or (direction == "short" and bias == "BULLISH")
    if aligned:
        score = min(100, base + 15)
        detail = f"macro bias {bias} aligns with a {direction} signal (risk {risk_level}, score {risk_score})"
    elif opposed:
        score = max(0, base - 25)
        detail = f"macro bias {bias} opposes a {direction} signal (risk {risk_level}, score {risk_score})"
    else:
        score = base
        detail = f"macro bias {bias or 'NEUTRAL'} neutral to a {direction} signal (risk {risk_level}, score {risk_score})"
    return {"score": round(score, 1), "detail": detail}


def _regime_component(regime_trend, regime_adx, direction):
    if regime_trend is None or direction is None:
        return None
    aligned = (direction == "long" and regime_trend == "Bullish") or (direction == "short" and regime_trend == "Bearish")
    opposed = (direction == "long" and regime_trend == "Bearish") or (direction == "short" and regime_trend == "Bullish")
    choppy = regime_trend == "Sideways/Choppy"
    adx_ok = (regime_adx or 0) >= TREND_ADX_THRESHOLD
    if choppy or not adx_ok:
        score, detail = 30, f"choppy/weak-trend regime (trend={regime_trend}, ADX={regime_adx}) -- low conviction for any direction"
    elif aligned:
        score, detail = 85, f"regime trend {regime_trend} (ADX {regime_adx}) aligns with a {direction} signal"
    elif opposed:
        score, detail = 15, f"regime trend {regime_trend} (ADX {regime_adx}) opposes a {direction} signal"
    else:
        score, detail = 50, f"regime trend {regime_trend} (ADX {regime_adx}) neutral to a {direction} signal"
    return {"score": score, "detail": detail}


def _sector_rs_lookup(sector_rotation_data):
    """sector name -> rs_vs_nifty_pct, from ALL 10 sectors (all_sectors_
    ranked), not just the top-3 stock_picks_by_sector subset -- covers
    every equity/stock-F&O symbol with a known sector, not only the lucky
    few in a currently-leading sector's representative picks."""
    ranked = (sector_rotation_data or {}).get("all_sectors_ranked", [])
    if not isinstance(ranked, list):
        return {}
    return {s["sector"]: s.get("rs_vs_nifty_pct") for s in ranked if isinstance(s, dict) and "sector" in s}


def _sector_component(rs_vs_nifty_pct, direction):
    """Phase C (added 09-Jul-2026): rs_vs_nifty_pct is a sector's own 5-day
    ROC% minus NIFTY50's over the same window (sector_rotation_core.py) --
    a plain difference, not a calibrated percentile rating. None if the
    symbol's sector is unresolved (equity_scan_core._ticker_sector_map()
    doesn't cover it, or sector_rotation.json is unavailable/stale) --
    never a fabricated neutral score."""
    if rs_vs_nifty_pct is None or direction is None:
        return None
    aligned = (direction == "long" and rs_vs_nifty_pct > 0) or (direction == "short" and rs_vs_nifty_pct < 0)
    opposed = (direction == "long" and rs_vs_nifty_pct < 0) or (direction == "short" and rs_vs_nifty_pct > 0)
    magnitude = min(abs(rs_vs_nifty_pct) * 5, 35)  # a 7pp+ ROC divergence saturates the swing
    if aligned:
        score, verb = min(100.0, 50 + magnitude), "outperforming"
    elif opposed:
        score, verb = max(0.0, 50 - magnitude), "underperforming"
    else:
        score, verb = 50.0, "tracking"
    detail = f"sector {verb} NIFTY by {rs_vs_nifty_pct:+.2f}% (5d ROC) -- {'aligns with' if aligned else 'opposes' if opposed else 'neutral to'} a {direction} signal"
    return {"score": round(score, 1), "detail": detail}


def _detect_earnings_shock(ticker):
    """Best-effort: an unusually large single-day price move on unusually
    heavy volume within the last EARNINGS_SHOCK_LOOKBACK_DAYS sessions --
    could be an earnings reaction, could be any other news, this project has
    no way to distinguish (no earnings-calendar data source exists here).
    Returns None on ANY fetch/data problem (never fabricates a "no shock"
    read from missing data) or the most recent qualifying day's stats."""
    try:
        df, _source = get_ohlcv(ticker, period="2mo")
    except Exception:
        return None
    if df is None or df.empty or len(df) < 25:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    close, vol = df["close"], df["volume"]
    if len(vol[vol > 0]) < 21:
        return None

    return_pct = close.pct_change() * 100
    vol_nan_zero = vol.replace(0, None)
    rel_volume = vol_nan_zero / vol_nan_zero.rolling(20, min_periods=10).mean().shift(1)

    recent = pd.DataFrame({"return_pct": return_pct, "rel_volume": rel_volume}).iloc[-EARNINGS_SHOCK_LOOKBACK_DAYS:]
    shocks = recent[(recent["return_pct"].abs() >= EARNINGS_SHOCK_MIN_ABS_RETURN_PCT)
                     & (recent["rel_volume"] >= EARNINGS_SHOCK_MIN_REL_VOLUME)]
    if shocks.empty:
        return None
    last_shock_date = shocks.index[-1]
    row = shocks.iloc[-1]
    return {
        "date": str(last_shock_date.date()),
        "return_pct": round(float(row["return_pct"]), 2),
        "rel_volume": round(float(row["rel_volume"]), 2),
        "days_ago": (df.index[-1] - last_shock_date).days,
    }


def _instrument_quality_component(vote_count, max_votes, extra_detail=None, earnings_shock=None):
    if vote_count is None or not max_votes:
        return None
    score = round(min(100.0, max(0.0, vote_count / max_votes * 100)), 1)
    detail = f"{vote_count}/{max_votes} votes" + (f" ({extra_detail})" if extra_detail else "")
    if earnings_shock:
        # Caution flag, not an override -- shave a modest, named amount off
        # the vote-based score rather than silently zeroing a setup that
        # might still be genuinely good.
        score = max(0.0, score - EARNINGS_SHOCK_SCORE_PENALTY)
        detail += (f" -- CAUTION: {earnings_shock['return_pct']:+.1f}% move on {earnings_shock['rel_volume']}x volume "
                   f"{earnings_shock['days_ago']}d ago, possible earnings/news shock still working through the signal")
    return {"score": round(score, 1), "detail": detail}


def _trade_quality_component(candidate, regime_dict, recent_trades, now_dt):
    """Calls the pure trade_filters._run_filters() (extracted 09-Jul-2026
    specifically so this per-refresh scorer never triggers evaluate_trade()'s
    logging/Telegram-alert side effects). Scored, never gates -- see module
    docstring."""
    try:
        results = _run_filters(candidate, regime=regime_dict, recent_trades=recent_trades, now_dt=now_dt)
    except Exception:
        return None
    if not results:
        return None
    passed = sum(1 for r in results if r["passed"])
    score = round(passed / len(results) * 100, 1)
    detail = "; ".join(f"{r['filter']}:{'OK' if r['passed'] else 'FAIL'}" for r in results)
    return {"score": score, "detail": detail, "filters": results}


def _learning_history_component(strategy_perf, kind, vote_count, macro_risk_level):
    """Looks up the most specific matching slice of strategy_performance.json
    -- macro_risk_level first (once Phase B ships; by_macro_risk_level is
    simply absent/empty until then, handled via .get()), then vote_count,
    then kind. insufficient_sample mirrors strategy_performance.py's own
    <20-decisive threshold -- never invents confidence from a thin bucket."""
    if not strategy_perf:
        return None
    by_macro = strategy_perf.get("by_macro_risk_level", {}) or {}
    by_vote = strategy_perf.get("by_vote_count", {}) or {}
    by_kind = strategy_perf.get("by_kind", {}) or {}

    bucket, basis = None, None
    if macro_risk_level is not None and str(macro_risk_level) in by_macro:
        bucket, basis = by_macro[str(macro_risk_level)], f"macro_risk_level={macro_risk_level}"
    elif vote_count is not None and str(vote_count) in by_vote:
        bucket, basis = by_vote[str(vote_count)], f"vote_count={vote_count}"
    elif kind in by_kind:
        bucket, basis = by_kind[kind], f"kind={kind}"
    if bucket is None:
        return None

    decisive = bucket.get("decisive", 0) or 0
    if decisive < MIN_DECISIVE_FOR_LEARNING_HISTORY:
        return {"insufficient_sample": True,
                "detail": f"only {decisive} decisive recs for {basis} -- treat as unproven, not scored"}
    win_rate = bucket.get("win_rate_pct")
    if win_rate is None:
        return None
    return {"score": round(win_rate, 1), "detail": f"{basis}: {win_rate}% win rate over {decisive} decisive recs"}


def _weighted_score(components):
    available = {k: v for k, v in components.items() if v is not None and "score" in v}
    inputs_used = {k: (k in available) for k in COMPONENT_WEIGHTS}
    if not available:
        return None, inputs_used
    total_weight = sum(COMPONENT_WEIGHTS[k] for k in available)
    if total_weight <= 0:
        return None, inputs_used
    score = sum(available[k]["score"] * COMPONENT_WEIGHTS[k] for k in available) / total_weight
    return round(score, 1), inputs_used


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------

def _score_bucket_state(score, raw_actionable):
    if not raw_actionable:
        return "WATCH", "raw signal not actionable this refresh"
    if score is None:
        return "SETUP_FORMING", "actionable but insufficient data to score confidently -- treat as forming, not confirmed"
    if score < SCORE_NO_TRADE_BELOW:
        return "NO_TRADE", f"context_score {score} below the no-trade floor ({SCORE_NO_TRADE_BELOW}) despite an actionable raw signal"
    if score >= SCORE_CONFIRMED_ENTRY_AT_OR_ABOVE:
        return "CONFIRMED_ENTRY", (f"context_score {score} >= {SCORE_CONFIRMED_ENTRY_AT_OR_ABOVE} -- "
                                    f"single-refresh read, NOT a persisted confirmation like expert_gate's")
    return "SETUP_FORMING", f"context_score {score} between {SCORE_NO_TRADE_BELOW} and {SCORE_CONFIRMED_ENTRY_AT_OR_ABOVE}"


def _find_open_rec(open_by_key, kind, symbol):
    for (k, s, _d), rec in open_by_key.items():
        if k == kind and s == symbol:
            return rec
    return None


def _recent_closed_loss(journal_recs, kind, symbol, now_date, lookback_days):
    for r in journal_recs:
        if (r.get("kind") == kind and r.get("symbol") == symbol
                and r.get("status") in ("lost", "invalidated") and r.get("closed_at")):
            closed = _parse_ist_date(r["closed_at"])
            if closed is not None and 0 <= (now_date - closed).days <= lookback_days:
                return r
    return None


def _stateless_lifecycle_state(kind, symbol, current_consensus, open_by_key, journal_recs, score, raw_actionable, now_dt):
    """fo_stock/equity: derive HOLD/EXIT_WATCH from a REAL open recommendation_
    journal.json entry where one exists (real data, not fabricated
    persistence), COOLDOWN from a real recent lost/invalidated one, and only
    fall back to a stateless score-threshold bucket otherwise. See module
    docstring / plan doc for why this leans on real journal data instead of
    inventing a second persisted state machine like expert_gate's."""
    open_rec = _find_open_rec(open_by_key, kind, symbol)
    if open_rec is not None:
        if current_consensus is not None and _flip_is_against(open_rec, current_consensus):
            return "EXIT_WATCH", (f"open rec (direction {open_rec.get('direction')}, opened {open_rec.get('opened_at')}) "
                                   f"-- signal has turned against it (now {current_consensus}) -- "
                                   f"single-refresh read, not persisted like expert_gate's EXIT_WATCH")
        return "HOLD", f"open recommendation since {open_rec.get('opened_at')} (direction {open_rec.get('direction')})"

    recent_loss = _recent_closed_loss(journal_recs, kind, symbol, now_dt.date(), COOLDOWN_LOOKBACK_DAYS)
    if recent_loss is not None:
        return "COOLDOWN", (f"a {recent_loss['status']} rec for this symbol closed {recent_loss.get('closed_at')} "
                             f"(within {COOLDOWN_LOOKBACK_DAYS}d) -- calendar-day approximation, not expert_gate's refresh-counted cooldown")

    return _score_bucket_state(score, raw_actionable)


def _recent_results_from_journal(journal_recs, kind, limit=10):
    """Kind-specific loss-streak history sourced from recommendation_journal
    .json (covers all 3 kinds) rather than trade_filters.py's own default
    (trade_journal.json, F&O paper-trades only) -- more representative for a
    scorer that also covers equity. Most-recent-first, matching filter_loss_
    streak's own documented input contract."""
    closed = [r for r in journal_recs if r.get("kind") == kind and r.get("status") in ("won", "lost")]
    closed.sort(key=lambda r: r.get("closed_at") or "", reverse=True)
    return [{"result": "win" if r["status"] == "won" else "loss"} for r in closed[:limit]]


def _first_or_none(items):
    for x in items:
        return x
    return None


# ---------------------------------------------------------------------------
# Per-kind processing
# ---------------------------------------------------------------------------

def _process_fo_index(fo, expert_gate_data, macro, regime, strategy_perf, now_dt):
    eg_instruments = (expert_gate_data or {}).get("instruments", {})
    regime_instruments = (regime or {}).get("instruments", {})
    entries = {}

    for sym in FO_INDEX_INSTRUMENTS:
        sig = (fo or {}).get(sym)
        valid = isinstance(sig, dict) and "error" not in sig
        raw = sig.get("consensus") if valid else None
        direction = "long" if raw == "BUY_CE" else "short" if raw == "BUY_PE" else None
        votes = None
        if valid and raw in ("BUY_CE", "BUY_PE"):
            votes = sig.get("ce_votes") if raw == "BUY_CE" else sig.get("pe_votes")
        trade = (sig.get("trade") if valid else None) or {}

        eg = eg_instruments.get(sym) or {}
        eg_state = eg.get("state", "WATCH")
        state = STATE_DISPLAY_MAP.get(eg_state, eg_state)
        state_reason = eg.get("reason", "no expert_gate data this run")

        macro_blocked, macro_why = direction_blocked(macro, raw) if raw in ("BUY_CE", "BUY_PE") else (False, None)
        hard_blocked, hard_block_reason = False, None
        if macro_blocked:
            hard_blocked, hard_block_reason = True, macro_why
            if eg_state == "WATCH":
                state = "NO_TRADE"
                state_reason = f"macro blocks this direction: {macro_why}"

        regime_inst = regime_instruments.get(sym) or {}
        candidate = {"symbol": sym, "strategy": None, "product": "MIS", "direction": "BUY",
                     "entry": trade.get("entry_premium"), "stop": trade.get("sl_premium"),
                     "target": trade.get("target1_premium")}

        components = {
            "macro": _macro_component(macro, direction),
            "regime": _regime_component(regime_inst.get("trend"), regime_inst.get("adx"), direction),
            "sector": _sector_component(None, None),  # no sector concept for an index
            "instrument_quality": _instrument_quality_component(votes, 6, extra_detail="index 3-factor consensus"),
            "trade_quality": _trade_quality_component(candidate, regime, None, now_dt) if direction else None,
            "learning_history": _learning_history_component(strategy_perf, "fo_index", votes, (macro or {}).get("risk_level")),
        }
        score, inputs_used = _weighted_score(components)

        entries[sym] = {
            "kind": "fo_index", "symbol": sym, "raw_signal": raw or "WAIT",
            "state": state, "state_engine": "expert_gate_persistent", "state_reason": state_reason,
            "context_score": score, "components": components,
            "gating": {"hard_blocked": hard_blocked, "hard_block_reason": hard_block_reason, "advisory_flags": []},
            "inputs_used": inputs_used,
            "data_as_of": sig.get("data_as_of") or sig.get("fetched_at") if valid else None,
        }
    return entries


def _process_fo_stock(stock_fo, macro, regime, journal_recs, strategy_perf, open_by_key, sector_rs, ticker_sector, now_dt):
    regime_inst = ((regime or {}).get("instruments", {})).get("NIFTY50") or {}  # broad-market proxy, matches recommendation_tracker._regime_for()
    entries = {}

    for sym, sig in (stock_fo or {}).items():
        if sym == "_meta" or not isinstance(sig, dict) or "error" in sig:
            continue
        raw = sig.get("consensus")
        raw_actionable = raw in ("BUY_CE", "BUY_PE")
        direction = "long" if raw == "BUY_CE" else "short" if raw == "BUY_PE" else None
        votes = sig.get("votes")
        trade = sig.get("trade") or {}

        if not raw_actionable and _find_open_rec(open_by_key, "fo_stock", sym) is None:
            continue  # nothing actionable and no position to track -- skip entirely

        macro_blocked, macro_why = direction_blocked(macro, raw) if raw_actionable else (False, None)

        if trade.get("type") == "single_leg":
            entry, stop, target = trade.get("entry_premium"), trade.get("sl_premium"), trade.get("target1_premium")
        else:  # spread, or no trade at all -- filter_risk_reward fails closed on missing data, which is honest
            entry, stop, target = None, None, None
        candidate = {"symbol": sym, "strategy": None, "product": "MIS", "direction": "BUY",
                     "entry": entry, "stop": stop, "target": target}
        recent = _recent_results_from_journal(journal_recs, "fo_stock")

        # 14/18 tracked stock-F&O tickers resolve to a sector via the same
        # equity_scan_core._ticker_sector_map() equity already uses; LT/
        # BAJFINANCE/TITAN/BHARTIARTL don't appear in any of the 10 sectors
        # -- ticker_sector.get(sym) is None for those, sector stays None,
        # never fabricated. Not expanding SECTOR_STOCKS' taxonomy here --
        # separate scope, see plan doc.
        sector = ticker_sector.get(sym)
        rs = sector_rs.get(sector) if sector else None
        shock = _detect_earnings_shock(sym + ".NS")

        components = {
            "macro": _macro_component(macro, direction),
            "regime": _regime_component(regime_inst.get("trend"), regime_inst.get("adx"), direction),
            "sector": _sector_component(rs, direction),
            "instrument_quality": _instrument_quality_component(votes, 6, extra_detail="stock 3-factor consensus", earnings_shock=shock),
            "trade_quality": _trade_quality_component(candidate, regime, recent, now_dt) if direction else None,
            "learning_history": _learning_history_component(strategy_perf, "fo_stock", votes, (macro or {}).get("risk_level")),
        }
        score, inputs_used = _weighted_score(components)
        state, state_reason = _stateless_lifecycle_state("fo_stock", sym, raw, open_by_key, journal_recs, score, raw_actionable, now_dt)

        advisory_flags = ["macro_adverse_direction"] if macro_blocked else []

        entries[sym] = {
            "kind": "fo_stock", "symbol": sym, "raw_signal": raw or "WAIT",
            "state": state, "state_engine": "stateless_v1", "state_reason": state_reason,
            "context_score": score, "components": components,
            "gating": {"hard_blocked": False, "hard_block_reason": None, "advisory_flags": advisory_flags},
            "inputs_used": inputs_used, "data_as_of": sig.get("data_as_of"),
        }
    return entries


def _process_equity(equity, macro, regime, journal_recs, strategy_perf, open_by_key, sector_rs, ticker_sector, now_dt):
    regime_inst = ((regime or {}).get("instruments", {})).get("NIFTY50") or {}
    entries = {}

    for sym, sig in (equity or {}).items():
        if sym == "_meta" or not isinstance(sig, dict):
            continue
        signal = sig.get("signal")
        raw_actionable = signal in ("BUY", "STRONG_BUY")

        if not raw_actionable and _find_open_rec(open_by_key, "equity", sym) is None:
            continue

        direction = "long" if raw_actionable else None  # equity is long-only in this system
        voters_dict = sig.get("strategies", {}) or {}
        acting_voters = [n for n, v in voters_dict.items() if v in ("BUY", "STRONG_BUY")]
        buy_votes = sig.get("buy_votes")
        sector_flag = sig.get("sector_macro_flag")  # avoid/watch/None, already computed by equity_scan_core.py

        # filter_market_regime checks a single "strategy" string against
        # market_regime.json's avoid-list; equity can have multiple acting
        # voters simultaneously, so this uses the first acting voter as a
        # representative check, not an exhaustive per-voter one -- documented
        # simplification, not fabrication (it's still a real voter name).
        candidate = {"symbol": sym, "strategy": _first_or_none(acting_voters), "product": "CNC", "direction": "BUY",
                     "entry": sig.get("entry"), "stop": sig.get("sl"), "target": sig.get("t1")}
        recent = _recent_results_from_journal(journal_recs, "equity")

        sector = ticker_sector.get(sym)
        rs = sector_rs.get(sector) if sector else None
        shock = _detect_earnings_shock(sym + ".NS")

        components = {
            "macro": _macro_component(macro, direction),
            "regime": _regime_component(regime_inst.get("trend"), regime_inst.get("adx"), direction),
            "sector": _sector_component(rs, direction),
            "instrument_quality": _instrument_quality_component(buy_votes, 4, extra_detail="of 4 named voters", earnings_shock=shock),
            "trade_quality": _trade_quality_component(candidate, regime, recent, now_dt) if direction else None,
            "learning_history": _learning_history_component(strategy_perf, "equity", buy_votes, (macro or {}).get("risk_level")),
        }
        score, inputs_used = _weighted_score(components)
        state, state_reason = _stateless_lifecycle_state("equity", sym, signal, open_by_key, journal_recs, score, raw_actionable, now_dt)

        advisory_flags = [f"sector_{sector_flag}"] if sector_flag else []

        entries[sym] = {
            "kind": "equity", "symbol": sym, "raw_signal": signal or "WATCH",
            "state": state, "state_engine": "stateless_v1", "state_reason": state_reason,
            "context_score": score, "components": components,
            "gating": {"hard_blocked": False, "hard_block_reason": None, "advisory_flags": advisory_flags},
            "inputs_used": inputs_used, "data_as_of": sig.get("data_as_of"),
        }
    return entries


# ---------------------------------------------------------------------------
# Pure entry point (no file I/O) -- context_score_dryrun.py calls this
# directly against reconstructed historical inputs. main() below is a thin
# I/O wrapper around it, same split as expert_gate.py's advance()/main().
# ---------------------------------------------------------------------------

def compute_context_score(fo, stock_fo, equity, regime, expert_gate_data, macro, journal_recs, strategy_perf, now_dt, sector_rotation=None):
    open_by_key = {_open_key(r): r for r in journal_recs if r.get("status") == "open"}
    sector_rs = _sector_rs_lookup(sector_rotation)
    ticker_sector = _ticker_sector_map()
    return {
        "fo_index": _process_fo_index(fo, expert_gate_data, macro, regime, strategy_perf, now_dt),
        "fo_stock": _process_fo_stock(stock_fo, macro, regime, journal_recs, strategy_perf, open_by_key, sector_rs, ticker_sector, now_dt),
        "equity": _process_equity(equity, macro, regime, journal_recs, strategy_perf, open_by_key, sector_rs, ticker_sector, now_dt),
    }


def main():
    now = now_ist()
    now_str = now_ist_str()

    fo = _load_json(FO_FILE, {})
    stock_fo = _load_json(STOCK_FO_FILE, {})
    equity = _load_json(EQUITY_FILE, {})
    regime = _load_json(REGIME_FILE, {})
    sector_rotation = _load_json(SECTOR_ROTATION_FILE, {})
    expert_gate_data = _load_json(EXPERT_GATE_FILE, {})
    strategy_perf = _load_json(STRATEGY_PERF_FILE, {})
    macro = load_macro_risk()

    journal_data = _load_json(JOURNAL_FILE, {})
    journal_recs = journal_data.get("recommendations", []) if isinstance(journal_data, dict) else []
    if not isinstance(journal_recs, list):
        journal_recs = []

    per_kind = compute_context_score(fo, stock_fo, equity, regime, expert_gate_data, macro, journal_recs, strategy_perf, now, sector_rotation=sector_rotation)

    inputs_available = {
        "macro": macro is not None,
        "regime": bool((regime or {}).get("instruments")),
        "expert_gate": bool((expert_gate_data or {}).get("instruments")),
        "strategy_performance": bool(strategy_perf),
        "recommendation_journal": isinstance(journal_data, dict) and "recommendations" in journal_data,
        "sector_rotation": bool((sector_rotation or {}).get("all_sectors_ranked")),
    }

    result = {
        "generated_at": now_str,
        "fo_index": per_kind["fo_index"],
        "fo_stock": per_kind["fo_stock"],
        "equity": per_kind["equity"],
        "inputs_available": inputs_available,
        "disclaimer": (
            "Combines existing macro/regime/vote/trade-quality/learning-history signals into one "
            "read per instrument. gating.hard_blocked is only ever true for fo_index, and only "
            "mirrors expert_gate.py's/macro_gate.py's own existing block -- fo_stock and equity are "
            "informational/advisory only by explicit design, never suppressed here. Not wired into "
            "any executor (none exists -- SEBI static-IP block) and NOT yet wired into the live CI "
            "pipeline -- see context_score_dryrun.py for the manual-review step required before it "
            "joins the refresh cycle. Educational only, not investment advice."
        ),
    }
    OUT_FILE.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    for kind in ("fo_index", "fo_stock", "equity"):
        for sym, e in result[kind].items():
            print(f"  [{kind}] {sym}: {e['state']} (score={e['context_score']}) -- {e['state_reason']}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  ERROR in context_score main(): {e} -- feed left unchanged this run")
