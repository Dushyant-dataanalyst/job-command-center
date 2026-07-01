"""
Snapshots each trading day's JSON data files into snapshots/YYYY-MM-DD/ for
easy rollback if a bad value ever slips past validation and gets committed.
Runs once daily (same 9pm IST prep gating as equity_scan_refresh.py) rather
than every 5 min — a snapshot is only useful once the day's data has settled,
and 96 snapshots/day of near-identical files would bloat the repo for no
benefit over the git history that already exists.
"""
import sys, os, shutil, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist

REPO_ROOT = pathlib.Path(__file__).parent.parent
SNAPSHOT_ROOT = REPO_ROOT / "snapshots"

FILES_TO_SNAPSHOT = [
    "fo_latest.json", "trade_journal.json", "sector_rotation.json",
    "market_mood.json", "news_feed.json", "equity_journal.json",
    "stock_fo.json", "equity_scan.json", "equity_scan_history.json",
    "market_regime.json",
]

KEEP_DAYS = 30  # older snapshots are pruned — git history is the real long-term record


def main():
    today = now_ist().strftime("%Y-%m-%d")
    out_dir = SNAPSHOT_ROOT / today
    out_dir.mkdir(parents=True, exist_ok=True)

    copied = []
    for filename in FILES_TO_SNAPSHOT:
        src = REPO_ROOT / filename
        if src.exists():
            shutil.copy2(src, out_dir / filename)
            copied.append(filename)
    print(f"  Snapshotted {len(copied)} files to {out_dir}")

    # Prune snapshots older than KEEP_DAYS
    if SNAPSHOT_ROOT.exists():
        from datetime import datetime, timedelta
        cutoff = now_ist().replace(tzinfo=None) - timedelta(days=KEEP_DAYS)
        pruned = 0
        for d in SNAPSHOT_ROOT.iterdir():
            if not d.is_dir():
                continue
            try:
                day = datetime.strptime(d.name, "%Y-%m-%d")
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(d)
                pruned += 1
        if pruned:
            print(f"  Pruned {pruned} snapshot(s) older than {KEEP_DAYS} days")


if __name__ == "__main__":
    main()
