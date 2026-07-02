"""
Shared Telegram alerting — send_alert(message, level) per the Master Brief's
spec (NSE_Trading_System_Master_Brief.md, Part 2/3).

Levels:
  INFO     — fills, daily summary (and now, every routine refresh)
  WARNING  — filter blocks, token expiring
  CRITICAL — kill switch triggered, dead man's switch, API errors

"CRITICAL alerts must never fail silently" (brief, Part 2) — if the Telegram
POST itself fails for a CRITICAL alert, it's appended to
logs/alert_failures.json for the dashboard to surface, since a failed
CRITICAL send is exactly the scenario where nobody finding out is the
actual danger, not just an inconvenience.
"""
import os, json, pathlib

import requests

from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
FALLBACK_LOG = REPO_ROOT / "logs" / "alert_failures.json"
FALLBACK_MAX = 100

# No emoji in the prefix text — matches the rest of this codebase, which
# avoids emoji in anything that might print to a Windows cp1252 console.
# Telegram itself renders emoji fine; this is just about print() not crashing
# when someone runs a script locally on Windows, same lesson learned earlier.
# SIGNAL is not in the Master Brief's original 3-level spec (INFO/WARNING/
# CRITICAL) — added for actionable trade-opportunity pushes (e.g. a 4/4
# unanimous equity signal) that are neither routine (INFO) nor a problem
# (WARNING/CRITICAL), so they don't get lost in the noise of every-run pings.
LEVEL_PREFIX = {"INFO": "[INFO]", "WARNING": "[WARNING]", "CRITICAL": "[CRITICAL]", "SIGNAL": "[SIGNAL]"}


def _append_fallback_log(level, message, error):
    FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        log = json.loads(FALLBACK_LOG.read_text(encoding="utf-8")) if FALLBACK_LOG.exists() else []
        if not isinstance(log, list):
            log = []
    except Exception:
        log = []
    log.append({"timestamp": now_ist_str(), "level": level, "message": message, "error": str(error)})
    log = log[-FALLBACK_MAX:]
    FALLBACK_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")


def send_alert(message, level="INFO"):
    """Returns True if the Telegram send succeeded, False otherwise."""
    prefix = LEVEL_PREFIX.get(level, level)
    text = f"{prefix} {message}"

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"  (Telegram not configured -- skipping {level} alert)")
        if level == "CRITICAL":
            _append_fallback_log(level, message, "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured")
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  {level} alert sent")
            return True
        print(f"  {level} alert failed: {resp.status_code} {resp.text}")
        if level == "CRITICAL":
            _append_fallback_log(level, message, f"{resp.status_code} {resp.text}")
        return False
    except Exception as e:
        print(f"  {level} alert failed: {e}")
        if level == "CRITICAL":
            _append_fallback_log(level, message, str(e))
        return False
