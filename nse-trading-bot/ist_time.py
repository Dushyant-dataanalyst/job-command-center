"""
Timezone-correct "now" for IST-labeled timestamps.

Every refresh script used to call bare datetime.now().strftime("...IST"),
which returns the RUNNER's local time — on GitHub Actions (ubuntu-latest,
default UTC) that's actually UTC mislabeled as IST, a consistent 5.5-hour
error. It only looked correct when testing locally on a machine already
set to IST. Confirmed via a real CI commit: fo_latest.json claimed
"01 Jul 2026 06:36 IST" while the commit's actual timestamp was 06:37 UTC.

Use now_ist_str() everywhere a "generated_at"/"fetched_at" IST string is
written, regardless of what timezone the machine running the script is in.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    return datetime.now(IST)


def now_ist_str():
    return now_ist().strftime("%d %b %Y %H:%M IST")
