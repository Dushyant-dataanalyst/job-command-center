"""
Pre-market health check — runs once before market open to confirm the
system is actually ready to trade off of, not just "probably fine."
Writes health_check.json for the dashboard to surface prominently, since
no external Telegram/Discord/email channel is wired up here (would need
credentials this assistant won't handle) — the dashboard itself is the
"all clear" / "issue found" notification surface.

Checks:
  1. yfinance connectivity — can we actually fetch live data right now?
  2. Last commit recency — did the automated pipeline produce a fresh
     commit recently (catches the exact "cron silently stopped" failure
     mode that prompted this check)?
  3. fo_latest.json staleness — is the core F&O signal file fresh enough
     to trust once the market opens?
"""
import sys, os, json, pathlib, subprocess
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist, now_ist_str
from yf_retry import download_with_retry

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "health_check.json"
FO_FILE = REPO_ROOT / "fo_latest.json"

STALE_COMMIT_HOURS = 20  # last automated commit should be within the previous trading day + prep run
STALE_FO_HOURS = 20


def _check_yfinance():
    try:
        import pandas as pd
        df = download_with_retry("^NSEI", period="5d")
        if df.empty:
            return False, "yfinance returned empty data for ^NSEI"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return True, f"OK - last close {float(df['close'].iloc[-1]):.2f}"
    except Exception as e:
        return False, f"yfinance fetch failed: {e}"


def _check_last_commit():
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%aI"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return False, "git log failed"
        from datetime import datetime, timezone
        last_commit = datetime.fromisoformat(out.stdout.strip())
        age_hours = (datetime.now(timezone.utc) - last_commit).total_seconds() / 3600
        ok = age_hours <= STALE_COMMIT_HOURS
        return ok, f"last commit {age_hours:.1f}h ago" + ("" if ok else f" — exceeds {STALE_COMMIT_HOURS}h threshold, pipeline may be stuck")
    except Exception as e:
        return False, f"could not check git log: {e}"


def _check_fo_freshness():
    if not FO_FILE.exists():
        return False, "fo_latest.json does not exist"
    try:
        d = json.loads(FO_FILE.read_text(encoding="utf-8"))
        ts = (d.get("_meta") or {}).get("generated_at")
        if not ts:
            return False, "fo_latest.json has no generated_at timestamp"
        from datetime import datetime
        import re
        m = re.match(r"(\d{1,2}) (\w{3}) (\d{4}) (\d{1,2}):(\d{2})", ts)
        if not m:
            return False, f"could not parse timestamp: {ts}"
        MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
        dt = datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)), int(m.group(4)), int(m.group(5)))
        age_hours = (now_ist().replace(tzinfo=None) - dt).total_seconds() / 3600
        ok = age_hours <= STALE_FO_HOURS
        return ok, f"fo_latest.json is {age_hours:.1f}h old" + ("" if ok else f" — exceeds {STALE_FO_HOURS}h threshold")
    except Exception as e:
        return False, f"could not check fo_latest.json: {e}"


def main():
    checks = {}
    checks["yfinance_connectivity"] = _check_yfinance()
    checks["last_commit_recency"] = _check_last_commit()
    checks["fo_data_freshness"] = _check_fo_freshness()

    all_ok = all(ok for ok, _ in checks.values())
    result = {
        "checked_at": now_ist_str(),
        "status": "ALL CLEAR" if all_ok else "ISSUE FOUND",
        "checks": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()},
    }
    OUT_FILE.write_text(json.dumps(result, indent=2))
    print(f"  Status: {result['status']}")
    for name, (ok, detail) in checks.items():
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
