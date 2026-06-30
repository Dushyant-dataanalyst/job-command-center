"""
Equity Trading Brain — live P&L for tracked swing positions.

Mirrors trade_brain.py's diagnostic spirit but for equities, which don't
auto-close like options (no expiry) — this tracks ongoing live status
rather than opening/closing simulated trades. Every run fetches a real
current price via yfinance and computes real P&L since entry, not a
signal-vote proxy (that's what _positionCommentary() in the dashboard
already does — this is the complementary, price-based half).

TRACKED_POSITIONS mirrors the dashboard's hardcoded DEFAULT_POSITIONS
(nse_live_dashboard.html) — kept in sync manually, the same pattern as
SECTOR_STOCKS in sector_rotation_core.py. Ad-hoc positions added via
"I BOUGHT" on the dashboard are read from my_positions.json if present
(see save-positions sync mechanism) and merged in, deduped by symbol.

With only a handful of positions, this does NOT attempt the
vote-strength statistical clustering trade_brain.py does (not enough
sample size to mean anything) — diagnostics here are purely descriptive:
best/worst performer, count above/below SL, total unrealized P&L.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

import pandas as pd

from yf_retry import download_with_retry

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "equity_journal.json"
SYNCED_POSITIONS_FILE = REPO_ROOT / "my_positions.json"

# Mirrors DEFAULT_POSITIONS in nse_live_dashboard.html — keep in sync manually.
TRACKED_POSITIONS = [
    {"name": "HDFCBANK",  "entry": 796.30,  "sl": 777.34,  "t1": 820.00,  "t2": 843.70,  "bought_at": "25 Jun 2026"},
    {"name": "ICICIBANK", "entry": 1387.50, "sl": 1355.15, "t1": 1427.94, "t2": 1468.38, "bought_at": "25 Jun 2026"},
    {"name": "KOTAKBANK", "entry": 409.00,  "sl": 400.01,  "t1": 420.24,  "t2": 431.47,  "bought_at": "25 Jun 2026"},
    {"name": "SBIN",      "entry": 1045.40, "sl": 1023.07, "t1": 1073.32, "t2": 1101.23, "bought_at": "25 Jun 2026"},
]


def _load_synced_positions():
    """Ad-hoc positions added via 'I BOUGHT' on the dashboard, synced through
    api/save-positions.js. Returns [] if the sync file doesn't exist yet —
    that's a normal state, not an error (sync may not be set up)."""
    if not SYNCED_POSITIONS_FILE.exists():
        return []
    try:
        data = json.loads(SYNCED_POSITIONS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _merged_tracked_positions():
    merged = {p["name"]: p for p in TRACKED_POSITIONS}
    for p in _load_synced_positions():
        if isinstance(p, dict) and p.get("name"):
            merged.setdefault(p["name"], p)  # defaults win if somehow duplicated
    return list(merged.values())


def _live_price(symbol):
    df = download_with_retry(symbol + ".NS", period="5d")
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    return round(float(df["close"].iloc[-1]), 2)


def main():
    now_str = datetime.now().strftime("%d %b %Y %H:%M IST")
    tracked = _merged_tracked_positions()
    results = []
    errors = {}

    for p in tracked:
        name = p["name"]
        price = _live_price(name)
        if price is None:
            errors[name] = "no live price data"
            continue
        entry = p.get("entry") or 0
        sl = p.get("sl") or 0
        t1 = p.get("t1") or 0
        t2 = p.get("t2") or 0
        pnl_pct = round((price - entry) / entry * 100, 2) if entry else None

        if sl and price <= sl:
            status = "below_sl"
        elif t2 and price >= t2:
            status = "target2_hit"
        elif t1 and price >= t1:
            status = "target1_hit"
        else:
            status = "healthy"

        results.append({
            "name": name,
            "entry": entry, "sl": sl, "t1": t1, "t2": t2,
            "current_price": price,
            "pnl_pct": pnl_pct,
            "status": status,
            "bought_at": p.get("bought_at", "—"),
            "source": "default" if any(d["name"] == name for d in TRACKED_POSITIONS) else "synced",
        })

    valid = [r for r in results if r["pnl_pct"] is not None]
    total_pnl_pct = round(sum(r["pnl_pct"] for r in valid) / len(valid), 2) if valid else None
    best = max(valid, key=lambda r: r["pnl_pct"], default=None)
    worst = min(valid, key=lambda r: r["pnl_pct"], default=None)
    below_sl = [r["name"] for r in valid if r["status"] == "below_sl"]

    if not valid:
        diagnostic = "No live price data available — check yfinance connectivity."
    elif len(valid) < 3:
        diagnostic = f"Only {len(valid)} tracked positions — too few to draw a pattern, but here's where things stand: " + \
            ", ".join(f"{r['name']} {'+' if r['pnl_pct']>=0 else ''}{r['pnl_pct']}%" for r in valid) + "."
    else:
        diagnostic = f"{best['name']} is your best performer at {'+' if best['pnl_pct']>=0 else ''}{best['pnl_pct']}%, " \
                     f"{worst['name']} is your worst at {'+' if worst['pnl_pct']>=0 else ''}{worst['pnl_pct']}%."
        if below_sl:
            diagnostic += f" {', '.join(below_sl)} {'is' if len(below_sl)==1 else 'are'} below stop-loss — review your exit plan."

    journal = {
        "fetched_at": now_str,
        "positions": results,
        "errors": errors,
        "stats": {
            "total_tracked": len(tracked),
            "total_pnl_pct": total_pnl_pct,
            "best": {"name": best["name"], "pnl_pct": best["pnl_pct"]} if best else None,
            "worst": {"name": worst["name"], "pnl_pct": worst["pnl_pct"]} if worst else None,
            "below_sl_count": len(below_sl),
            "diagnostic": diagnostic,
        },
        "disclaimer": "Real positions, real prices — not paper trades. yfinance EOD/delayed quotes; verify live price in your broker terminal before acting.",
    }

    OUT_FILE.write_text(json.dumps(journal, indent=2))
    print(f"  tracked={len(tracked)} priced={len(valid)} total_pnl={total_pnl_pct}% errors={errors}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
