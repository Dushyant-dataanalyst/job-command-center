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
`if:` to consume. workflow_dispatch (manual "Run workflow" clicks) always
runs -- forcing a refresh on demand should never be silently skipped.
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

IST = ZoneInfo("Asia/Kolkata")


def _is_market_hours(now):
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    mins = now.hour * 60 + now.minute
    return MARKET_OPEN_MIN <= mins <= MARKET_CLOSE_MIN


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


def decide(now, is_manual_dispatch):
    if is_manual_dispatch:
        return True, "manual workflow_dispatch"
    market = _is_market_hours(now)
    if market:
        return True, "market hours"
    gap = _minutes_since_last_run(now)
    if gap is None:
        return True, "no prior run on record"
    if gap >= OFF_HOURS_MIN_GAP_MINUTES:
        return True, f"off-hours, {gap:.0f} min since last run (>= {OFF_HOURS_MIN_GAP_MINUTES})"
    return False, f"off-hours, only {gap:.0f} min since last run (< {OFF_HOURS_MIN_GAP_MINUTES}) -- skipping, hourly cadence off-hours"


def main():
    now = now_ist()
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    should_run, reason = decide(now, is_manual)

    print(f"  now={now.strftime('%a %d %b %H:%M IST')} should_run={should_run} ({reason})")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"should_run={'true' if should_run else 'false'}\n")


if __name__ == "__main__":
    main()
