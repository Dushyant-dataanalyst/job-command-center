"""
News & Corporate Events tracker — NSE official RSS feeds, free, no signup.

Pulls two feeds (verified live, plain XML, just needs a browser User-Agent
header — no API key, no session/cookie dance):
  - Online_announcements.xml — general corporate announcements
  - Board_Meetings.xml — upcoming board meeting intimations

This is corporate-announcement data (results, board meetings, trading-window
closures, appointments, etc.) — NOT general market/macro news. NSE doesn't
publish a free general-news feed; that gap is intentionally deferred (see
NEXT UPGRADE below).

Tracked-ticker list used to be a hardcoded 4-entry dict frozen at whatever
the dashboard's original DEFAULT_POSITIONS were — it silently went stale
the moment any of those got removed (all 4 had been, by 02 Jul 2026, so
position_news was coming back empty regardless of real news). Now built
dynamically every run from:
  1. Real current holdings — DEFAULT_POSITION_NAMES minus my_positions.json's
     removedDefaults, plus my_positions.json's ad-hoc positions, minus
     anything in closedTrades.
  2. System B's long-term watchlist (quality_growth_watchlist.json), so
     "news around your long-term holdings/watchlist" is covered too, not
     just active swing positions.
Ticker -> legal-name resolution (needed because NSE announcement titles are
the full legal company name, not the ticker) is done via yfinance and
cached to _ticker_name_cache.json so this doesn't re-hit yfinance for a
name that never changes on every 5-min run.

NEXT UPGRADE (not built — needs a signup): general market headlines via
NewsAPI.org (free tier: 100 req/day) as a second source feeding the same
news_feed.json shape (a "general_headlines" key alongside "position_news"
and "recent_announcements" below).
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import xml.etree.ElementTree as ET

import requests

from ist_time import now_ist_str

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "news_feed.json"
MY_POSITIONS_FILE = REPO_ROOT / "my_positions.json"
LT_WATCHLIST_FILE = REPO_ROOT / "quality_growth_watchlist.json"
NAME_CACHE_FILE = pathlib.Path(__file__).parent / "_ticker_name_cache.json"

FEEDS = {
    "Online_announcements": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "Board_Meetings": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Mirrors nse_live_dashboard.html's DEFAULT_POSITIONS names — same source of
# truth intentionally duplicated (the dashboard's list is JS embedded in
# HTML, not readable from Python without a fragile parse). Update both
# together if the defaults ever change.
DEFAULT_POSITION_NAMES = ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "SBIN"]


def _fetch_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"company": title, "summary": desc, "link": link, "published": pub})
    return items


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _current_holdings():
    data = _load_json(MY_POSITIONS_FILE, {})
    removed = set(data.get("removedDefaults", []))
    closed = {t.get("name") for t in data.get("closedTrades", []) if t.get("name")}
    adhoc = {p.get("name") for p in data.get("positions", []) if p.get("name")}
    defaults_active = {n for n in DEFAULT_POSITION_NAMES if n not in removed}
    return (defaults_active | adhoc) - closed


def _long_term_watchlist_tickers():
    data = _load_json(LT_WATCHLIST_FILE, {})
    return {r["ticker"].replace(".NS", "") for r in data.get("ranked", []) if r.get("ticker")}


def _resolve_names(tickers, cache):
    """Fills in any missing ticker->legal-name entries via yfinance, only
    for tickers not already cached — company names don't change often
    enough to justify re-fetching every 5-min run."""
    missing = [t for t in tickers if t not in cache]
    if not missing:
        return False
    import yfinance as yf
    for t in missing:
        try:
            symbol = t if t.endswith(".NS") else f"{t}.NS"
            info = yf.Ticker(symbol).info
            name = info.get("longName") or info.get("shortName")
            if name:
                cache[t] = name
        except Exception:
            pass  # leave unresolved — that ticker just won't be matchable against announcements this run
    return True


def main():
    fetched_at = now_ist_str()
    all_items = []
    feed_errors = {}
    for name, url in FEEDS.items():
        try:
            all_items.extend(_fetch_feed(url))
        except Exception as e:
            feed_errors[name] = str(e)

    holdings = _current_holdings()
    long_term = _long_term_watchlist_tickers()
    tracked_tickers = sorted(holdings | long_term)

    cache = _load_json(NAME_CACHE_FILE, {})
    if _resolve_names(tracked_tickers, cache):
        NAME_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    position_names = {t: cache[t] for t in tracked_tickers if t in cache}

    position_news = []
    recent_announcements = []
    for item in all_items:
        matched_ticker = next((t for t, name in position_names.items() if name.lower() in item["company"].lower()), None)
        if matched_ticker:
            kind = "holding" if matched_ticker in holdings else "watchlist"
            position_news.append({**item, "ticker": matched_ticker, "kind": kind})
        else:
            recent_announcements.append(item)

    result = {
        "fetched_at": fetched_at,
        "source": "NSE official RSS (nsearchives.nseindia.com) — corporate announcements, not general market news",
        "position_news": position_news[:15],
        "recent_announcements": recent_announcements[:15],
        "feed_errors": feed_errors,
        "tracked_positions": sorted(holdings),
        "tracked_watchlist": sorted(long_term),
        "disclaimer": "Corporate filings/announcements only (results, board meetings, trading-window closures, etc.), not macro/general market news. General-news API integration deferred — see NEXT UPGRADE in script docstring.",
    }

    OUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  holdings={sorted(holdings)} watchlist_count={len(long_term)}")
    print(f"  position_news={len(position_news)} recent={len(recent_announcements)} errors={feed_errors}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
