"""
Expert Gate (learning-engine roadmap item 3/5) — a state machine that sits
between a RAW index-F&O signal and calling it a trade-worthy setup, to cut
the CE/PE whipsaw the user complained about. Raw consensus flips every time
the 3-factor score crosses a line; this refuses to treat every flip as an
entry or a panic exit.

SCOPE: NIFTY50 + BANKNIFTY index F&O only (that's where the flipping problem
and the alerts live). Stock F&O / equity are out of scope for v1 -- the same
machine could extend to them later.

STATES (per instrument): WATCH -> SETUP_FORMING -> CONFIRMED_ENTRY ->
IN_TRADE -> EXIT_WATCH -> EXIT_CONFIRMED -> COOLDOWN -> WATCH.

RULES (all tunable constants below, nothing magic buried in logic):
  - Entry needs the SAME actionable direction to persist ENTRY_PERSIST_
    REFRESHES consecutive refreshes (kills 1-tick flips).
  - Entry is BLOCKED in a choppy / low-ADX regime (reads market_regime.json;
    Sideways/Choppy or ADX < MIN_ADX_FOR_ENTRY = no confirmation).
  - Entry is BLOCKED in the first/last 15 min of the session (reuses
    config.py's SESSION_OPEN/CLOSE + BLOCK_FIRST/LAST_MINUTES -- same single
    source of truth trade_filters.py uses) and outside session hours.
  - "BUY_CE -> WAIT" is EXIT_WATCH, not instant panic: an exit needs to
    persist EXIT_PERSIST_REFRESHES before EXIT_CONFIRMED. A recovery back to
    the held direction cancels the exit.
  - Reversing CE<->PE needs STRONGER evidence: a straight flip to the
    opposite side only fast-tracks to exit if the opposite side has MORE
    votes than the held side had at entry; a weaker opposite just starts
    EXIT_WATCH.
  - After any exit/flip, COOLDOWN_REFRESHES before a new entry can confirm.

WHAT CONSUMES THIS: the gate writes expert_gate.json (a dashboard feed) with
each instrument's state + two one-shot booleans, entry_confirmed_this_run /
exit_confirmed_this_run. signal_alerts.py consults those so F&O *entry*
alerts fire on a CONFIRMED entry, not a raw flip -- and it FAILS OPEN (if
this feed is missing/stale/broken, signal_alerts reverts to raw-flip
alerting, so a gate bug can never make the phone go silent on a real
signal). Real-held-position EXIT alerts are deliberately NOT gated (those
protect live capital -- suppressing them behind a persistence delay is the
wrong risk trade).

IMPORTANT: this gate does NOT place, size, or authorize any trade -- there
is no executor (SEBI static-IP block). It only decides which raw signals are
"confirmed setups" for alerting + (future) paper-trading + (much later)
execution. Per the user's principle: expert observer -> expert paper trader
-> only much later executor.

State persists in logs/expert_gate_state.json (committed CI state, NOT a
dashboard feed -- same status as logs/alert_state.json). The advance() core
is pure (all inputs passed in) so the state machine is unit-testable without
live data, same pattern as should_run_full_refresh.decide().
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import time as dtime

from ist_time import now_ist, now_ist_str
import config

REPO_ROOT = pathlib.Path(__file__).parent.parent
FO_FILE = REPO_ROOT / "fo_latest.json"
REGIME_FILE = REPO_ROOT / "market_regime.json"
STATE_FILE = REPO_ROOT / "logs" / "expert_gate_state.json"
OUT_FILE = REPO_ROOT / "expert_gate.json"

INSTRUMENTS = ("NIFTY50", "BANKNIFTY")

# --- tunables (documented so "be stricter/looser" is a one-line change) ---
ENTRY_PERSIST_REFRESHES = 2   # same actionable direction must hold this many consecutive refreshes
EXIT_PERSIST_REFRESHES = 2    # exit condition must hold this many before EXIT_CONFIRMED
COOLDOWN_REFRESHES = 3        # block new entry this many refreshes after an exit/flip
MIN_ADX_FOR_ENTRY = 20        # below this (or Sideways/Choppy trend) = no entry confirmation
MIN_VOTES_FOR_ENTRY = 4       # raw index consensus is already >=4/6; raise to 5 to "prefer 5/6+ setups"


def _load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_hhmm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _within_entry_time_window(now):
    """True only inside the session and outside its first/last N minutes --
    reuses config.py's window (single source of truth with trade_filters.py)."""
    if now.weekday() >= 5:
        return False
    t = now.time()
    open_t = _parse_hhmm(config.SESSION_OPEN)
    close_t = _parse_hhmm(config.SESSION_CLOSE)
    def _mins(x):
        return x.hour * 60 + x.minute
    first_end = _mins(open_t) + config.BLOCK_FIRST_MINUTES_OF_SESSION
    last_start = _mins(close_t) - config.BLOCK_LAST_MINUTES_OF_SESSION
    now_m = _mins(t)
    return first_end <= now_m <= last_start


def _regime_ok(regime_inst):
    if not isinstance(regime_inst, dict):
        return False, "no regime data (fail closed on entry)"
    trend = regime_inst.get("trend")
    adx = regime_inst.get("adx")
    if trend == "Sideways/Choppy":
        return False, "choppy/sideways regime"
    if adx is None or adx < MIN_ADX_FOR_ENTRY:
        return False, f"ADX {adx} < {MIN_ADX_FOR_ENTRY} (no reliable trend)"
    return True, f"trend {trend}, ADX {adx}"


def _blank_state():
    return {"state": "WATCH", "direction": None, "persist_count": 0,
            "exit_persist_count": 0, "cooldown_until": 0, "entry_votes": None,
            "entered_at": None, "last_update": None, "reason": "initial"}


def advance(raw_consensus, votes, regime_inst, now, st, refresh_counter):
    """PURE state-machine step. Returns (new_state_dict, entry_confirmed,
    exit_confirmed). raw_consensus in {BUY_CE, BUY_PE, WAIT}; votes is
    {ce, pe}; regime_inst is market_regime.json's dict for this instrument;
    now is an IST datetime; st is this instrument's prior state dict;
    refresh_counter is the monotonic gate-run count."""
    s = dict(st)
    entry_confirmed = False
    exit_confirmed = False
    actionable = raw_consensus in ("BUY_CE", "BUY_PE")
    cur_votes = (votes or {}).get("ce" if raw_consensus == "BUY_CE" else "pe") if actionable else None

    def set_state(name, reason, **kw):
        s["state"] = name
        s["reason"] = reason
        s.update(kw)

    # --- COOLDOWN: block until the counter passes, then drop to WATCH ---
    if s["state"] == "COOLDOWN":
        if refresh_counter >= s.get("cooldown_until", 0):
            set_state("WATCH", "cooldown elapsed", direction=None, persist_count=0, exit_persist_count=0)
        else:
            remaining = s["cooldown_until"] - refresh_counter
            s["reason"] = f"cooldown, {remaining} refresh(es) left"
            s["last_update"] = now_ist_str()
            return s, entry_confirmed, exit_confirmed

    if s["state"] in ("WATCH", "SETUP_FORMING"):
        if not actionable:
            set_state("WATCH", "signal WAIT/none", direction=None, persist_count=0)
        else:
            if s.get("direction") == raw_consensus:
                s["persist_count"] = s.get("persist_count", 0) + 1
            else:
                s["direction"] = raw_consensus
                s["persist_count"] = 1
            reg_ok, reg_why = _regime_ok(regime_inst)
            time_ok = _within_entry_time_window(now)
            votes_ok = (cur_votes or 0) >= MIN_VOTES_FOR_ENTRY
            persist_ok = s["persist_count"] >= ENTRY_PERSIST_REFRESHES
            if persist_ok and reg_ok and time_ok and votes_ok:
                set_state("CONFIRMED_ENTRY",
                          f"confirmed: {raw_consensus} held {s['persist_count']} refreshes, {reg_why}, {cur_votes} votes",
                          entry_votes=cur_votes, entered_at=now_ist_str(), exit_persist_count=0)
                entry_confirmed = True
            else:
                blocks = []
                if not persist_ok: blocks.append(f"persist {s['persist_count']}/{ENTRY_PERSIST_REFRESHES}")
                if not reg_ok: blocks.append(reg_why)
                if not time_ok: blocks.append("outside entry time window")
                if not votes_ok: blocks.append(f"{cur_votes} < {MIN_VOTES_FOR_ENTRY} votes")
                set_state("SETUP_FORMING", f"{raw_consensus} forming -- waiting on: " + "; ".join(blocks))

    elif s["state"] == "CONFIRMED_ENTRY":
        # one-shot state; next refresh we're holding
        set_state("IN_TRADE", f"holding {s.get('direction')}", exit_persist_count=0)

    elif s["state"] == "IN_TRADE":
        direction = s.get("direction")
        if raw_consensus == direction:
            set_state("IN_TRADE", "thesis intact", exit_persist_count=0)
        elif raw_consensus == "WAIT":
            set_state("EXIT_WATCH", "signal went WAIT -- watching, not panicking", exit_persist_count=1)
        else:  # opposite actionable direction
            opp_votes = cur_votes or 0
            if opp_votes > (s.get("entry_votes") or 0):
                set_state("EXIT_CONFIRMED",
                          f"strong reversal to {raw_consensus} ({opp_votes} > {s.get('entry_votes')} entry votes)")
                exit_confirmed = True
            else:
                set_state("EXIT_WATCH",
                          f"weak reversal to {raw_consensus} ({opp_votes} <= {s.get('entry_votes')}) -- watching", exit_persist_count=1)

    elif s["state"] == "EXIT_WATCH":
        direction = s.get("direction")
        if raw_consensus == direction:
            set_state("IN_TRADE", "recovered to held direction", exit_persist_count=0)
        else:
            s["exit_persist_count"] = s.get("exit_persist_count", 0) + 1
            if s["exit_persist_count"] >= EXIT_PERSIST_REFRESHES:
                set_state("EXIT_CONFIRMED", f"exit condition held {s['exit_persist_count']} refreshes")
                exit_confirmed = True
            else:
                s["reason"] = f"exit-watch {s['exit_persist_count']}/{EXIT_PERSIST_REFRESHES}"

    elif s["state"] == "EXIT_CONFIRMED":
        set_state("COOLDOWN", f"cooling down {COOLDOWN_REFRESHES} refreshes after exit",
                  direction=None, persist_count=0, exit_persist_count=0,
                  cooldown_until=refresh_counter + COOLDOWN_REFRESHES)

    s["last_update"] = now_ist_str()
    return s, entry_confirmed, exit_confirmed


def main():
    now = now_ist()
    now_str = now_ist_str()
    fo = _load_json(FO_FILE, {})
    regime = _load_json(REGIME_FILE, {})
    state = _load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    refresh_counter = int(state.get("_refresh_counter", 0)) + 1
    regimes = regime.get("instruments", {}) if isinstance(regime, dict) else {}

    out_instruments = {}
    for inst in INSTRUMENTS:
        sig = fo.get(inst) if isinstance(fo, dict) else None
        if not isinstance(sig, dict) or "error" in sig:
            # no signal this run -- hold prior state untouched, just report it
            prior = state.get(inst, _blank_state())
            out_instruments[inst] = {**prior, "entry_confirmed_this_run": False,
                                     "exit_confirmed_this_run": False, "raw_consensus": None}
            continue
        raw = sig.get("consensus")
        votes = {"ce": sig.get("ce_votes"), "pe": sig.get("pe_votes")}
        st = state.get(inst, _blank_state())
        new_st, entered, exited = advance(raw, votes, regimes.get(inst), now, st, refresh_counter)
        state[inst] = new_st
        out_instruments[inst] = {**new_st, "entry_confirmed_this_run": entered,
                                 "exit_confirmed_this_run": exited, "raw_consensus": raw,
                                 "adx": (regimes.get(inst) or {}).get("adx"),
                                 "regime_trend": (regimes.get(inst) or {}).get("trend")}

    state["_refresh_counter"] = refresh_counter
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    result = {
        "generated_at": now_str,
        "refresh_counter": refresh_counter,
        "instruments": out_instruments,
        "params": {
            "entry_persist_refreshes": ENTRY_PERSIST_REFRESHES,
            "exit_persist_refreshes": EXIT_PERSIST_REFRESHES,
            "cooldown_refreshes": COOLDOWN_REFRESHES,
            "min_adx_for_entry": MIN_ADX_FOR_ENTRY,
            "min_votes_for_entry": MIN_VOTES_FOR_ENTRY,
        },
        "disclaimer": "Confirmation-and-cooldown state machine over the raw index-F&O signal, "
                      "to reduce CE/PE whipsaw. Does NOT place/size/authorize any trade (no executor "
                      "exists -- SEBI static-IP block). Educational signal-lifecycle layer only.",
    }
    OUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for inst, o in out_instruments.items():
        print(f"  {inst}: {o['state']}"
              + (f" [{o.get('direction')}]" if o.get("direction") else "")
              + (" ENTRY-CONFIRMED" if o.get("entry_confirmed_this_run") else "")
              + (" EXIT-CONFIRMED" if o.get("exit_confirmed_this_run") else "")
              + f" -- {o.get('reason')}")
    print(f"  refresh_counter={refresh_counter} | Wrote {OUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"  ERROR in expert_gate main(): {e} -- state/feed left as-is; signal_alerts fails open to raw-flip alerts")
