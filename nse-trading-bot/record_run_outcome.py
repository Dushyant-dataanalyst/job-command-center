"""
Records this CI run's step-by-step outcome to logs/run_history.json (capped
at the last 200 entries) and sends a Telegram alert on EVERY run — success or
failure — via the shared alerts.send_alert() primitive. Previously this only
alerted on failure; changed to alert every time so there's a live pulse of
the pipeline actually executing, rather than "phone stays silent = probably
fine" — which is exactly the assumption that let the earlier refresh gap
go unnoticed for hours.

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
        text = f"Refresh OK — all {len(outcomes)} steps succeeded at {now_str}"
        send_alert(text, level="INFO")


if __name__ == "__main__":
    main()
