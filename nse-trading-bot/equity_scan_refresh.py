"""
Equity scan refresh — writes equity_scan.json to the repo root, replacing
the hand-pasted SCAN_DATA blob that used to live directly in
nse_live_dashboard.html (last manually updated 25 Jun 2026, never
refreshed since). Runs once daily on the 9pm IST prep cron, not the 5-min
market-hours cron — a 46-stock multi-indicator scan is too heavy to repeat
every 5 minutes and swing signals don't need that granularity.

See equity_scan_core.py for the strategy logic and its reconstruction
disclaimer.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date

from ist_time import now_ist_str
from equity_scan_core import scan_universe

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "equity_scan.json"
HISTORY_FILE = REPO_ROOT / "equity_scan_history.json"
HISTORY_MAX_DAYS = 30


def _update_history(strong_buy, buy, watch):
    """Appends today's counts to a rolling window, so the dashboard's
    30-day trend chart has something real to plot instead of the old
    2-point hardcoded HIST_DATA. Dedupes by date so re-running the same
    day (e.g. manual dispatch) updates today's entry instead of adding a
    second one."""
    today = str(date.today())
    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8")) if HISTORY_FILE.exists() else []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history = [h for h in history if isinstance(h, dict) and h.get("date") != today]
    history.append({"date": today, "sb": strong_buy, "b": buy, "w": watch})
    history.sort(key=lambda h: h["date"])
    history = history[-HISTORY_MAX_DAYS:]
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def main():
    try:
        now_str = now_ist_str()
        results, errors = scan_universe(fetch_peg=True)
        strong_buy = sum(1 for r in results.values() if r["signal"] == "STRONG_BUY")
        buy = sum(1 for r in results.values() if r["signal"] == "BUY")
        watch = sum(1 for r in results.values() if r["signal"] == "WATCH")
        _update_history(strong_buy, buy, watch)

        out = {
            "_meta": {
                "generated_at": now_str,
                "source": "yfinance EOD",
                "universe": "SECTOR_STOCKS (sector_rotation_core.py) — 46 stocks / 10 sectors",
                "strategy_note": "Inna/Pham/Cianni/Unger reconstructed from short tooltip descriptions, "
                                  "not verified against an original spec. Educational signal engine only, "
                                  "not investment advice.",
                "counts": {"strong_buy": strong_buy, "buy": buy, "watch": watch, "total": len(results)},
                "errors": errors,
            },
        }
        out.update(results)
        OUT_FILE.write_text(json.dumps(out, indent=2))
        print(f"  scanned={len(results)} strong_buy={strong_buy} buy={buy} watch={watch} errors={len(errors)}")
        print(f"  Wrote {OUT_FILE}")
    except Exception as e:
        OUT_FILE.write_text(json.dumps({
            "_meta": {
                "generated_at": now_ist_str(),
                "error": str(e),
                "counts": {"strong_buy": 0, "buy": 0, "watch": 0, "total": 0},
            },
        }, indent=2))
        print(f"  ERROR in main(): {e} — wrote error-state JSON")


if __name__ == "__main__":
    main()
