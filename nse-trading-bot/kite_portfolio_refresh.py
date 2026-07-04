"""
Real Zerodha portfolio sync — writes kite_portfolio.json at repo root from
the paid Kite Connect account-read APIs (holdings, positions, margins,
today's trades/orders). This is the ₹2000/month subscription actually being
used for its portfolio data, not just a yfinance quote fallback.

Kite access_tokens expire ~6 AM IST daily and are refreshed manually
(kite_auth_refresh.yml). So this fails CLOSED and GRACEFUL: when there's no
valid same-day session, it writes {"session_live": false, ...} rather than
crashing, so the CI step stays green and the dashboard can show a "refresh
your Kite token" prompt instead of stale/blank data.

PRIVACY NOTE: kite_portfolio.json is served as a public static feed at the
(unauthenticated) dashboard URL — real holdings/P&L/available capital are
therefore readable by anyone with that URL. This is an accepted tradeoff
(user chose "keep it simple"); revisit with Vercel deployment protection or
a gated endpoint if that ever changes.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from ist_time import now_ist_str
from kite_fallback import (
    _load_session, get_holdings, get_positions, get_margins, get_trades, get_orders,
)
from equity_brain import _merged_tracked_positions

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "kite_portfolio.json"
TRADE_HISTORY_FILE = REPO_ROOT / "kite_trade_history.json"
TRADE_HISTORY_MAX = 2000  # generous cap for a personal account's trade volume over years, not just recent runs


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _append_trade_history(trades_raw):
    """Kite's own /trades API only ever returns TODAY's fills -- confirmed
    against Kite's docs ("order history... only lives for a day in the
    system"), no date-range parameter exists. So there is no way to ever
    retroactively recover a past week's real trades via the API; the only
    way to build genuine broker-confirmed trade history is to persist each
    day's real fills ourselves, starting now. This runs every 5 min (same
    cadence as the rest of the CI cron) and appends any trade_id not
    already logged, so a real trade is captured within minutes of
    executing, then never touched again (Kite fills don't change after
    the fact). Always writes the file (even [] on a no-trade day) so it
    exists reliably from the first run — matching every other JSON feed in
    this project and letting validate_json_outputs.py check it like any
    other. Returns the number of newly-logged trades."""
    try:
        history = json.loads(TRADE_HISTORY_FILE.read_text(encoding="utf-8")) if TRADE_HISTORY_FILE.exists() else []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    known_ids = {t.get("trade_id") for t in history if t.get("trade_id")}
    new_count = 0
    for t in (trades_raw or []):
        tid = t.get("trade_id")
        if tid and tid not in known_ids:
            history.append({**t, "logged_at": now_ist_str()})
            known_ids.add(tid)
            new_count += 1

    history = history[-TRADE_HISTORY_MAX:]
    TRADE_HISTORY_FILE.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return new_count


def _reconcile(holdings):
    """Compare Kite's real holdings against what the dashboard currently
    tracks (the same merged default+ad-hoc list equity_brain uses), so
    stale manual tracking is surfaced instead of silently drifting."""
    held = {h.get("tradingsymbol") for h in holdings if h.get("tradingsymbol")}
    tracked = {p["name"] for p in _merged_tracked_positions() if p.get("name")}
    return {
        "held_but_not_tracked": sorted(held - tracked),
        "tracked_but_not_held": sorted(tracked - held),
    }


def _slim_holding(h):
    # Kite's "quantity" is settled/free shares only -- a same-day buy sits in
    # "t1_quantity" (T+1 settlement) and would show quantity=0 despite being
    # a real, owned position with real P&L. Total actual holding is the sum;
    # Kite's own `pnl` already accounts for the full position either way.
    settled = _num(h.get("quantity"))
    pending = _num(h.get("t1_quantity"))
    qty = settled + pending
    avg = _num(h.get("average_price"))
    ltp = _num(h.get("last_price"))
    return {
        "tradingsymbol": h.get("tradingsymbol"),
        "exchange": h.get("exchange"),
        "quantity": qty,
        "pending_settlement": pending if pending else None,
        "average_price": round(avg, 2),
        "last_price": round(ltp, 2),
        "pnl": round(_num(h.get("pnl")), 2),
        "day_change_percentage": round(_num(h.get("day_change_percentage")), 2),
        "value": round(qty * ltp, 2),
        "invested": round(qty * avg, 2),
    }


def _unavailable(reason):
    payload = {
        "session_live": False,
        "fetched_at": now_ist_str(),
        "message": "Kite session expired or not set up — refresh your token "
                   "(kite_auth_refresh workflow) to see live holdings.",
        "reason": reason,
        "holdings": [],
        "positions": [],
        "margin_available": None,
        "today_trades": [],
        "today_orders": [],
        "reconciliation": {"held_but_not_tracked": [], "tracked_but_not_held": []},
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  session not live ({reason}) — wrote graceful session_live:false state")
    print(f"  Wrote {OUT_FILE}")


def main():
    if _load_session() is None:
        _unavailable("no valid same-day kite_session.json")
        return

    holdings_raw, h_err = get_holdings()
    positions_raw, _ = get_positions()
    margins_raw, _ = get_margins()
    trades_raw, _ = get_trades()
    orders_raw, _ = get_orders()

    # If even holdings failed on a session we thought was live, the token is
    # likely revoked/expired mid-day — treat as not-live rather than emit a
    # half-empty portfolio that looks real.
    if holdings_raw is None:
        _unavailable(h_err or "holdings request failed")
        return

    holdings = [_slim_holding(h) for h in holdings_raw]
    total_value = round(sum(h["value"] for h in holdings), 2)
    total_invested = round(sum(h["invested"] for h in holdings), 2)
    total_pnl = round(sum(h["pnl"] for h in holdings), 2)
    total_pnl_pct = round(total_pnl / total_invested * 100, 2) if total_invested else None

    net_positions = (positions_raw or {}).get("net", []) if isinstance(positions_raw, dict) else []
    open_positions = [p for p in net_positions if _num(p.get("quantity")) != 0]

    margin_available = None
    if isinstance(margins_raw, dict):
        eq = margins_raw.get("equity") or {}
        # 'net' is the usable balance; fall back to available.live_balance/cash.
        margin_available = eq.get("net")
        if margin_available is None:
            margin_available = (eq.get("available") or {}).get("live_balance")
        margin_available = round(_num(margin_available), 2) if margin_available is not None else None

    payload = {
        "session_live": True,
        "fetched_at": now_ist_str(),
        "holdings": holdings,
        "stats": {
            "total_value": total_value,
            "total_invested": total_invested,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "holdings_count": len(holdings),
        },
        "positions": open_positions,
        "margin_available": margin_available,
        "today_trades": trades_raw or [],
        "today_orders": orders_raw or [],
        "reconciliation": _reconcile(holdings_raw),
        "disclaimer": "Real Zerodha account data via Kite Connect. Read-only — no orders placed. "
                      "Verify in your Kite terminal before acting.",
    }
    OUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    recon = payload["reconciliation"]
    print(f"  holdings={len(holdings)} value=Rs{total_value} pnl=Rs{total_pnl} ({total_pnl_pct}%) margin=Rs{margin_available}")
    print(f"  reconcile: held_not_tracked={recon['held_but_not_tracked']} tracked_not_held={recon['tracked_but_not_held']}")
    print(f"  Wrote {OUT_FILE}")

    new_trades = _append_trade_history(trades_raw)
    if new_trades:
        print(f"  logged {new_trades} new real trade(s) to {TRADE_HISTORY_FILE}")


if __name__ == "__main__":
    main()
