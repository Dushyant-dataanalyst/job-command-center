"""
Astrological Market View -- built at the user's explicit request after
reviewing this exact tradeoff: a rival Telegram channel's astrology-flavored
call failed and was mocked by its own subscribers the same day its NIFTY PE
call worked, and even that channel's own pinned disclaimer says "make
entries based on technicals." The user chose to add this anyway, on the
condition it stays clearly labeled as unverified and never touches any
trade decision. Every design choice below exists to honor that condition
literally, not just in spirit.

TWO LAYERS, kept explicitly distinct in the output and in this docstring:

1. astronomical_facts -- REAL, VERIFIABLE, DETERMINISTIC data computed via
   pyephem (standard orbital-mechanics library, no API key, fully offline).
   Moon phase/illumination and Mercury's retrograde status are genuine
   astronomy: checkable against any almanac, not fabricated, not guessed.
   Mercury retrograde is computed HERE from actual geocentric ecliptic
   longitude (day-over-day motion direction), not a hardcoded date table --
   hardcoding specific 2026 retrograde dates without a live source to verify
   them against would itself have been exactly the kind of unverified claim
   this whole codebase's anti-hallucination design exists to avoid.

2. folk_market_view -- a DOCUMENTED TRADITIONAL BELIEF some retail
   astrology-influenced traders reference (e.g. "buy near new moon, sell
   near full moon"). This has NO established causal mechanism on equity
   prices. Academic work on lunar-cycle stock effects (e.g. Yuan, Zheng &
   Zhu, "Are Investors Moonstruck?", J. Empirical Finance 2006) finds at
   most a marginal, contested effect -- nowhere near reliable for trading.
   This layer is included ONLY because the user explicitly asked for it
   after being shown that caveat, and ONLY ever labeled as folk belief.

ISOLATION (the actual point of this module, per the user's own condition):
astro_view.json is NOT read by macro_risk_refresh.py, macro_gate.py,
expert_gate.py, trade_brain.py, stock_fo_refresh.py, or equity_scan_core.py
-- grep the whole repo, nothing in the trade-decision path imports this
file or this JSON. It contributes zero points to risk_score, blocks
nothing, sizes nothing. It is a standalone, once-daily, informational-only
feed -- gated to the once-daily prep run (not the 5-min cadence) because
moon phase and Mercury's retrograde status are both slow-moving facts that
do not meaningfully change within a trading day.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timedelta, timezone

import ephem

from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "astro_view.json"

# Market-astrology commentary conventionally centers on Mercury (ruling
# communication/contracts/trade in Western astrology) and the Moon
# (sentiment/volatility) -- the two most commonly cited factors by retail
# market-astrology sources, including the channel that prompted this
# module. Kept to just these two rather than all planets, matching the
# scope of what's actually being asked for.
MOON_PHASE_NAMES = [
    (1.84566, "New Moon"), (5.53699, "Waxing Crescent"), (9.22831, "First Quarter"),
    (12.91963, "Waxing Gibbous"), (16.61096, "Full Moon"), (20.30228, "Waning Gibbous"),
    (23.99361, "Last Quarter"), (27.68493, "Waning Crescent"), (29.53059, "New Moon"),
]


def _moon_phase_name(age_days):
    for threshold, name in MOON_PHASE_NAMES:
        if age_days < threshold:
            return name
    return "New Moon"


def _moon_facts(now_utc):
    obs = ephem.Observer()
    obs.lon, obs.lat = "77.2090", "28.6139"  # New Delhi -- NSE's home market
    obs.date = now_utc

    moon = ephem.Moon(obs)
    illumination_pct = round(float(moon.phase), 1)

    prev_new = ephem.previous_new_moon(now_utc).datetime()
    next_new = ephem.next_new_moon(now_utc).datetime()
    next_full = ephem.next_full_moon(now_utc).datetime()

    age_days = (now_utc.replace(tzinfo=None) - prev_new).total_seconds() / 86400
    waxing = next_full < next_new  # next full moon comes before next new moon => currently waxing
    phase_name = _moon_phase_name(age_days)

    return {
        "phase_name": phase_name,
        "illumination_pct": illumination_pct,
        "waxing": waxing,
        "age_days_since_new_moon": round(age_days, 1),
        "days_to_next_new_moon": round((next_new - now_utc.replace(tzinfo=None)).total_seconds() / 86400, 1),
        "days_to_next_full_moon": round((next_full - now_utc.replace(tzinfo=None)).total_seconds() / 86400, 1),
    }


def _mercury_apparent_motion(obs, on_date):
    """Day-over-day change in Mercury's geocentric ecliptic longitude --
    the actual astronomical definition of retrograde (negative = apparent
    backward motion against the background stars, a real observable
    phenomenon caused by Earth and Mercury's relative orbital speeds, not
    an astrological claim)."""
    obs.date = on_date - timedelta(days=1)
    mercury = ephem.Mercury(obs)
    lon_before = float(ephem.Ecliptic(mercury).lon)
    obs.date = on_date
    mercury = ephem.Mercury(obs)
    lon_after = float(ephem.Ecliptic(mercury).lon)
    delta = lon_after - lon_before
    if delta > 3.14159265:
        delta -= 2 * 3.14159265
    if delta < -3.14159265:
        delta += 2 * 3.14159265
    return delta


def _mercury_facts(now_utc):
    obs = ephem.Observer()
    obs.lon, obs.lat = "77.2090", "28.6139"
    currently_retrograde = _mercury_apparent_motion(obs, now_utc) < 0

    # Walk backward/forward (bounded -- retrograde windows run ~3 weeks,
    # direct windows up to ~4 months, so 150 days each direction is a safe
    # ceiling) to find this status's start and the next station (flip).
    days_in_status = 0
    for i in range(1, 151):
        check_date = now_utc - timedelta(days=i)
        if (_mercury_apparent_motion(obs, check_date) < 0) != currently_retrograde:
            days_in_status = i - 1
            break
    else:
        days_in_status = None  # status predates our lookback window

    next_station_date, days_to_next_station = None, None
    for i in range(1, 151):
        check_date = now_utc + timedelta(days=i)
        if (_mercury_apparent_motion(obs, check_date) < 0) != currently_retrograde:
            next_station_date = check_date.strftime("%d %b %Y")
            days_to_next_station = i
            break

    return {
        "status": "retrograde" if currently_retrograde else "direct",
        "days_in_current_status": days_in_status,
        "next_station_date": next_station_date,
        "next_station_type": ("goes direct" if currently_retrograde else "goes retrograde") if next_station_date else None,
        "days_to_next_station": days_to_next_station,
    }


def _folk_view(moon, mercury):
    """Traditional/folk interpretation layer -- explicitly NOT a causal
    claim, see module docstring. Every string here is written to read as
    a reported belief ('traditionally viewed as...'), never as a fact."""
    moon_bias = (
        f"Waxing moon ({moon['phase_name']}, {moon['illumination_pct']}% illuminated) -- "
        "some retail market-astrology sources treat the waxing half of the lunar cycle as "
        "a traditionally 'accumulating/bullish-leaning' period." if moon["waxing"] else
        f"Waning moon ({moon['phase_name']}, {moon['illumination_pct']}% illuminated) -- "
        "some retail market-astrology sources treat the waning half of the lunar cycle as "
        "a traditionally 'distributing/bearish-leaning' period."
    )
    if mercury["status"] == "retrograde":
        mercury_bias = (
            f"Mercury retrograde (day {mercury['days_in_current_status']} of this cycle, "
            f"{mercury['next_station_type']} around {mercury['next_station_date']}) -- traditionally "
            "associated in folk market-astrology with communication breakdowns, contract/paperwork "
            "delays, and choppier price action, most often cited for IT/Telecom/Media names. "
            "Not a documented basis for the 4-voter technical engine or the F&O consensus."
        )
    else:
        mercury_bias = (
            f"Mercury direct (day {mercury['days_in_current_status'] if mercury['days_in_current_status'] is not None else '?'} "
            f"of this cycle) -- no retrograde-associated caution in folk market-astrology right now."
        )
    return {
        "moon_bias": moon_bias,
        "mercury_bias": mercury_bias,
        "combined_note": "These two lines are a traditional belief, not a signal. They carry no weight anywhere "
                          "in this system's actual trade logic -- see isolation_notice below.",
    }


def main():
    now_utc = datetime.now(timezone.utc)
    fetched_at = now_ist_str()

    moon = _moon_facts(now_utc)
    mercury = _mercury_facts(now_utc)

    result = {
        "fetched_at": fetched_at,
        "astronomical_facts": {"moon": moon, "mercury": mercury},
        "folk_market_view": _folk_view(moon, mercury),
        "isolation_notice": "This feed is NOT read by macro_risk_refresh.py, macro_gate.py, expert_gate.py, "
                             "trade_brain.py, stock_fo_refresh.py, or equity_scan_core.py -- nothing in the "
                             "trade-decision path imports this file. It contributes zero points to risk_score, "
                             "blocks nothing, sizes nothing, and generates no trade or alert by itself.",
        "disclaimer": "ASTROLOGY HAS NO ESTABLISHED SCIENTIFIC OR CAUSAL MECHANISM FOR PREDICTING STOCK PRICES. "
                       "The astronomical_facts above (moon phase, Mercury's real orbital motion) are genuine, "
                       "verifiable, computed data (via pyephem) -- checkable against any almanac. The "
                       "folk_market_view below them is a traditional belief some retail astrology-influenced "
                       "traders reference; academic work on lunar-cycle stock effects (e.g. Yuan/Zheng/Zhu 2006, "
                       "'Are Investors Moonstruck?') finds at most a marginal, contested effect, nowhere near "
                       "reliable for trading. Entertainment/informational only -- never used to size, block, "
                       "or generate any trade in this system.",
    }

    OUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  Moon: {moon['phase_name']} ({moon['illumination_pct']}%, {'waxing' if moon['waxing'] else 'waning'})")
    print(f"  Mercury: {mercury['status']} (day {mercury['days_in_current_status']}, "
          f"next station {mercury['next_station_date']})")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        fetched_at = now_ist_str()
        OUT_FILE.write_text(json.dumps({
            "error": str(e), "fetched_at": fetched_at,
            "isolation_notice": "This feed is NOT read by any trade-decision code.",
            "disclaimer": "Astrological market view -- entertainment/informational only. Refresh failed this run.",
        }, indent=2), encoding="utf-8")
        print(f"  ERROR in main(): {e} -- wrote error-state JSON")
