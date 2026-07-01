"""
One-time-per-day Kite Connect session refresh.

Kite Connect's access_token is valid for a single trading day only (expires
~6 AM IST) — there is no long-lived API key alone that works. This script
exchanges a manually-obtained request_token for the day's access_token and
writes it to kite_session.json for the rest of the day's cron runs to read.

Deliberately NOT fully automated (no password/TOTP stored) — see the
kite_auth_refresh.yml workflow for the manual step this requires each
morning. This is a security tradeoff made explicitly: storing a Zerodha
account password + TOTP seed to eliminate a 30-second daily manual step
was judged not worth it for what is a secondary/fallback data source.

kite_session.json is git-tracked, not gitignored — the token self-expires
within ~24h regardless, and this repo is private, so the residual risk of
having it in git history is low. This avoids needing a second GitHub PAT
scoped to secrets:write just to rotate one token.
"""
import sys, os, json, pathlib, hashlib
sys.path.insert(0, os.path.dirname(__file__))

import requests

from ist_time import now_ist_str, now_ist

REPO_ROOT = pathlib.Path(__file__).parent.parent
SESSION_FILE = REPO_ROOT / "kite_session.json"

KITE_API_URL = "https://api.kite.trade/session/token"


def refresh_session(request_token):
    api_key = os.environ.get("KITE_API_KEY")
    api_secret = os.environ.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("KITE_API_KEY / KITE_API_SECRET not set in environment")

    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    resp = requests.post(KITE_API_URL, data={
        "api_key": api_key,
        "request_token": request_token,
        "checksum": checksum,
    }, headers={"X-Kite-Version": "3"}, timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"Kite token exchange failed: {resp.status_code} {resp.text}")

    data = resp.json().get("data", {})
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError(f"Kite response had no access_token: {resp.text}")

    session = {
        "access_token": access_token,
        "api_key": api_key,
        "refreshed_at": now_ist_str(),
        "trading_date": now_ist().strftime("%Y-%m-%d"),  # tokens are valid for this IST trading day only
    }
    SESSION_FILE.write_text(json.dumps(session, indent=2))
    print(f"  Kite session refreshed for {session['trading_date']}")
    print(f"  Wrote {SESSION_FILE}")


def main():
    request_token = os.environ.get("KITE_REQUEST_TOKEN")
    if not request_token:
        print("  ERROR: KITE_REQUEST_TOKEN not provided. See kite_auth_refresh.yml for the login URL to get one.")
        sys.exit(1)
    try:
        refresh_session(request_token)
    except Exception as e:
        print(f"  ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
