"""
Shared macro-risk gating helpers -- reused by expert_gate.py (index F&O
confirmation), trade_brain.py (index F&O paper-trade opens), and
stock_fo_refresh.py (stock F&O advisory signals) so "does macro block this
direction" and "how much should we escalate votes/persistence" are computed
identically everywhere, not reimplemented three times with room to drift.

POLICY (documented here since three call sites share it): anything that
autonomously commits simulated/tracked capital or flips a state machine --
trade_brain.py's paper-trade opens, expert_gate.py's CONFIRMED_ENTRY -- is
actually BLOCKED when macro says so. Pure advisory signal displays that a
human decides on manually (stock_fo.json's suggestions; equity has no
automated open at all) are FLAGGED, never hidden -- the macro overlay's own
disclaimer says it doesn't replace the technical signal, and hiding a
technical signal from a human decision-maker would do exactly that.

FAILS OPEN EVERYWHERE: any missing/unreadable/error-state macro_risk.json
means "no macro adjustment" -- callers get exactly the behavior they had
before this module existed. A macro bug can only ever make the pipeline
MORE conservative when the feed IS readable, never silent/broken when it
isn't (same fail-open contract expert_gate.py already uses for its own
missing-feed case).
"""
import json, pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent
MACRO_FILE = REPO_ROOT / "macro_risk.json"


def load_macro_risk():
    """None on any missing/unreadable/error-state feed -- callers must treat
    None as "no adjustment", never as "zero risk"."""
    try:
        data = json.loads(MACRO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("risk_score") is None:
        return None
    return data


def direction_blocked(macro, consensus):
    """consensus in {BUY_CE, BUY_PE}. CE = buying a call = a long/bullish
    bet, PE = buying a put = a short/bearish bet on the underlying -- mirrors
    the macro brief's own worked examples (BUY_CE + HIGH BEARISH -> blocked;
    BUY_PE + HIGH BEARISH -> allowed, just needs more confirmation). Returns
    (blocked: bool, reason: str|None)."""
    if macro is None or consensus not in ("BUY_CE", "BUY_PE"):
        return False, None
    adj = macro.get("trade_adjustments", {})
    if consensus == "BUY_CE" and adj.get("allow_new_longs") is False:
        return True, f"macro risk {macro.get('risk_level')}/{macro.get('bias')} blocks new longs (allow_new_longs=false)"
    if consensus == "BUY_PE" and adj.get("allow_new_shorts") is False:
        return True, f"macro risk {macro.get('risk_level')}/{macro.get('bias')} blocks new shorts (allow_new_shorts=false)"
    return False, None


def escalated(macro, default, key):
    """max(default, macro override) -- macro can only ever tighten a
    threshold, never loosen it below the caller's own default."""
    if macro is None:
        return default
    override = (macro.get("trade_adjustments") or {}).get(key)
    if override is None:
        return default
    return max(default, override)


def macro_context(macro):
    """Compact context to stamp onto a trade/signal record for later
    analysis. None if macro is unavailable -- callers write a null field
    rather than fabricating an 'unknown' block."""
    if macro is None:
        return None
    return {
        "risk_level": macro.get("risk_level"),
        "bias": macro.get("bias"),
        "risk_score": macro.get("risk_score"),
        "position_size_multiplier": (macro.get("trade_adjustments") or {}).get("position_size_multiplier"),
    }
