"""
Decides whether THIS trigger should do a full refresh, or just heartbeat
and skip — the mechanism behind "every 5 min during market hours, hourly
otherwise" that works regardless of what's actually triggering the
workflow (native GitHub schedule, or the external cron-job.org ping that
fires every 5 min around the clock).

Deliberately NOT based on which cron string fired (github.event.schedule
only exists for native schedule triggers, not workflow_dispatch calls from
cron-job.org) -- instead reads the real current IST time plus when the
pipeline last actually completed a run (logs/run_history.json), so the
same policy applies no matter how often or from where the trigger comes:
  - During market hours (Mon-Fri, same 07:30-16:25 IST window the existing
    5-min cron already targets): always run.
  - Outside that window (nights, weekends, holidays): only run if at least
    OFF_HOURS_MIN_GAP_MINUTES have passed since the last recorded run --
    throttling a 5-min (or more frequent) external trigger down to roughly
    hourly, without needing that external trigger's own schedule changed.

Writes should_run=true/false to $GITHUB_OUTPUT for the workflow's job-level
`if:` to consume.

BUG FIXED 08-Jul-2026: this used to treat ANY workflow_dispatch event as a
genuine manual "Run workflow" click (which should always bypass the
off-hours throttle) -- but cron-job.org's automated 24/7 ping ALSO fires
via workflow_dispatch (that's the whole reason it exists, see module
docstring above), so EVERY off-hours cron-job.org trigger was silently
treated as "manual" and bypassed the throttle entirely, defeating the
"hourly off-hours" cadence this whole module exists to enforce. This is
very likely the actual root cause of the original Actions-minutes billing
crisis documented in CLAUDE.md (the repo went public 06-Jul-2026 to escape
it) -- and since the plan is to revert to private ~01-Aug-2026 once the
quota resets, leaving this unfixed would silently recreate that exact
crisis the moment the repo goes private again. Fixed by reading an
explicit FORCE_FULL_REFRESH env var (sourced from a new
workflow_dispatch.inputs.force_full_refresh checkbox, default false) instead
of blindly trusting github.event_name -- cron-job.org's blind API calls
never set this input, so they now get the real time-based decision like
any other trigger; a genuine human using the GitHub UI can still tick the
box to force a run right now.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime
from zoneinfo import ZoneInfo

from ist_time import now_ist

REPO_ROOT = pathlib.Path(__file__).parent.parent
RUN_HISTORY_FILE = REPO_ROOT / "logs" / "run_history.json"

MARKET_OPEN_MIN = 7 * 60 + 30    # 07:30 IST -- matches the existing */5 cron window
MARKET_CLOSE_MIN = 16 * 60 + 25  # 16:25 IST
OFF_HOURS_MIN_GAP_MINUTES = 55   # a bit under an hour so trigger jitter doesn't skip a whole cycle

# Once-daily prep window -- centered on the '30 15 * * 1-5' cron (21:00 IST).
# Widened to a real clock-time window (not an exact-minute match) for the
# same reason should_run itself avoids matching on github.event.schedule:
# GitHub's native schedule trigger is documented best-effort and can skip a
# tick, and cron-job.org (the reliable backstop) only fires every 5 min, so
# an exact-minute match would miss both. 25 minutes comfortably guarantees
# at least one cron-job.org ping lands inside it even if the native
# schedule tick is skipped entirely.
DAILY_WINDOW_START_MIN = 20 * 60 + 55  # 20:55 IST
DAILY_WINDOW_END_MIN = 21 * 60 + 20    # 21:20 IST

IST = ZoneInfo("Asia/Kolkata")


def _is_market_hours(now):
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    mins = now.hour * 60 + now.minute
    return MARKET_OPEN_MIN <= mins <= MARKET_CLOSE_MIN


def _is_daily_window(now):
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return DAILY_WINDOW_START_MIN <= mins <= DAILY_WINDOW_END_MIN


def _minutes_since_last_run(now):
    """None means 'no prior run on record' -- treated as should-run, not
    should-skip, since there's nothing to throttle against yet."""
    if not RUN_HISTORY_FILE.exists():
        return None
    try:
        history = json.loads(RUN_HISTORY_FILE.read_text(encoding="utf-8"))
        if not history:
            return None
        last_ts = history[-1].get("timestamp")
        if not last_ts:
            return None
        dt = datetime.strptime(last_ts.replace(" IST", ""), "%d %b %Y %H:%M").replace(tzinfo=IST)
        return (now - dt).total_seconds() / 60
    except Exception:
        return None


def decide(now, force):
    """force = a genuine human ticked the 'force full refresh' box when
    manually running from the GitHub UI -- NOT just 'this was a
    workflow_dispatch event' (see BUG FIXED note in the module docstring)."""
    if force:
        return True, "forced (workflow_dispatch, force_full_refresh=true)"
    market = _is_market_hours(now)
    if market:
        return True, "market hours"
    gap = _minutes_since_last_run(now)
    if gap is None:
        return True, "no prior run on record"
    if gap >= OFF_HOURS_MIN_GAP_MINUTES:
        return True, f"off-hours, {gap:.0f} min since last run (>= {OFF_HOURS_MIN_GAP_MINUTES})"
    return False, f"off-hours, only {gap:.0f} min since last run (< {OFF_HOURS_MIN_GAP_MINUTES}) -- skipping, hourly cadence off-hours"


def decide_daily(now, force):
    """Whether the once-daily-only steps (equity scan, voter weights,
    snapshot, astro view, daily report) should run THIS trigger. Only
    meaningful when decide() above already returned True -- if the whole
    refresh is being skipped, there's nothing for the daily steps to
    attach to."""
    if force:
        return True, "forced (workflow_dispatch, force_full_refresh=true)"
    if _is_daily_window(now):
        return True, f"within once-daily window ({DAILY_WINDOW_START_MIN//60:02d}:{DAILY_WINDOW_START_MIN%60:02d}-{DAILY_WINDOW_END_MIN//60:02d}:{DAILY_WINDOW_END_MIN%60:02d} IST)"
    return False, "outside once-daily window -- daily-only steps skipped this trigger"


def main():
    now = now_ist()
    force = os.environ.get("FORCE_FULL_REFRESH", "").lower() == "true"
    should_run, reason = decide(now, force)
    should_run_daily, daily_reason = decide_daily(now, force)

    print(f"  now={now.strftime('%a %d %b %H:%M IST')} should_run={should_run} ({reason})")
    print(f"  should_run_daily={should_run_daily} ({daily_reason})")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if should_run else 'false'}\n")
            f.write(f"should_run_daily={'true' if should_run_daily else 'false'}\n")


if __name__ == "__main__":
    main()
