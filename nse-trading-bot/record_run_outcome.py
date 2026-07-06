"""
Records this CI run's step-by-step outcome to logs/run_history.json (capped
at the last 200 entries) and sends a Telegram alert ONLY on failure
(CRITICAL). The every-run INFO success ping that briefly lived here was
removed 06-Jul-2026 at the user's explicit request ("don't message me each
refresh") — with the 5-min market-hours cadence it meant 100+ pings/day of
pure noise. The two things that ping-per-run was protecting against are
covered elsewhere now: "pipeline broke loudly" -> the CRITICAL alert below;
"pipeline went silent" -> the healthchecks.io dead-man's-switch heartbeat
in the workflow's gate job. Actionable signal alerts (new strong buys,
exit triggers) live in signal_alerts.py, deduped via logs/alert_state.json.

Step outcomes are passed in as STEP_<name>=<success|failure|skipped|cancelled>
env vars by the workflow (GitHub Actions' own `steps.<id>.outcome` context,
one per Python step) — this script just reads whichever STEP_* vars are set,
so it doesn't need to know the step list in advance.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist_str
from alerts import send_alert

REPO_ROOT = pathlib.Path(__file__).parent.parent
LOG_FILE = REPO_ROOT / "logs" / "run_history.json"
HISTORY_MAX = 200


def _collect_step_outcomes():
    return {
        k[len("STEP_"):]: v
        for k, v in os.environ.items()
        if k.startswith("STEP_") and v
    }


def _append_run_history(entry):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        history = json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history.append(entry)
    history = history[-HISTORY_MAX:]
    LOG_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")


def main():
    outcomes = _collect_step_outcomes()
    failed = [name for name, outcome in outcomes.items() if outcome == "failure"]
    now_str = now_ist_str()

    entry = {
        "timestamp": now_str,
        "event": os.environ.get("GITHUB_EVENT_NAME", "unknown"),
        "steps": outcomes,
        "any_failed": bool(failed),
    }
    _append_run_history(entry)
    print(f"  Recorded run outcome: {len(outcomes)} steps, {len(failed)} failed")
    print(f"  Wrote {LOG_FILE}")

    run_url = os.environ.get("GITHUB_RUN_URL", "")
    if failed:
        # A step reaching "failure" here means it crashed past its own
        # internal try/except (most scripts already swallow their own errors
        # into an error-state JSON and exit 0) — an unusual, CRITICAL-grade
        # event per the Master Brief's own "API errors" example.
        text = (
            f"Refresh at {now_str} had {len(failed)} failure(s): "
            + ", ".join(failed)
            + (f"\n{run_url}" if run_url else "")
        )
        send_alert(text, level="CRITICAL")
    else:
        # Deliberately NO Telegram on success — see docstring (user request
        # 06-Jul-2026). Run history + Actions logs keep the full record.
        print(f"  All {len(outcomes)} steps succeeded — no alert sent (by design)")


if __name__ == "__main__":
    main()
