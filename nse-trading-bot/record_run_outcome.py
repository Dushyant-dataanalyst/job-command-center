"""
Records this CI run's step-by-step outcome to logs/run_history.json (capped
at the last 200 entries) and sends a Telegram alert if anything failed.

Step outcomes are passed in as STEP_<name>=<success|failure|skipped|cancelled>
env vars by the workflow (GitHub Actions' own `steps.<id>.outcome` context,
one per Python step) — this script just reads whichever STEP_* vars are set,
so it doesn't need to know the step list in advance.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import requests

from ist_time import now_ist_str

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


def _send_telegram_alert(failed_steps, run_url):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("  (Telegram not configured — TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping alert)")
        return
    text = (
        "[WARNING] NSE dashboard refresh had failures:\n"
        + "\n".join(f"- {name}" for name in failed_steps)
        + (f"\n\n{run_url}" if run_url else "")
    )
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if resp.status_code == 200:
            print("  Telegram alert sent")
        else:
            print(f"  Telegram alert failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  Telegram alert failed: {e}")


def main():
    outcomes = _collect_step_outcomes()
    failed = [name for name, outcome in outcomes.items() if outcome == "failure"]

    entry = {
        "timestamp": now_ist_str(),
        "event": os.environ.get("GITHUB_EVENT_NAME", "unknown"),
        "steps": outcomes,
        "any_failed": bool(failed),
    }
    _append_run_history(entry)
    print(f"  Recorded run outcome: {len(outcomes)} steps, {len(failed)} failed")
    print(f"  Wrote {LOG_FILE}")

    if failed:
        run_url = os.environ.get("GITHUB_RUN_URL", "")
        _send_telegram_alert(failed, run_url)


if __name__ == "__main__":
    main()
