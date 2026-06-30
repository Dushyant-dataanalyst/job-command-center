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

Filters to companies in a small, manually-verified ticker->legal-name map
(_POSITION_NAMES below) covering the actual tracked positions. Matching is
case-insensitive substring on the announcement title (NSE announcement
titles are always the full legal company name). Unmatched recent items are
kept separately as "Recent Market Announcements" (unfiltered) so nothing
is silently hidden by an incomplete name map.

NEXT UPGRADE (not built — needs a signup): general market headlines via
NewsAPI.org (free tier: 100 req/day) as a second source feeding the same
news_feed.json shape (a "general_headlines" key alongside "position_news"
and "recent_announcements" below).
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

import xml.etree.ElementTree as ET
from datetime import datetime

import requests

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "news_feed.json"

FEEDS = {
    "Online_announcements": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "Board_Meetings": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Manually-verified ticker -> legal name substring, for the real tracked positions.
# NSE announcement titles are always the full legal company name, so a
# substring match here is reliable (not a guess at fuzzy matching).
_POSITION_NAMES = {
    "HDFCBANK":  "HDFC Bank",
    "ICICIBANK": "ICICI Bank",
    "SBIN":      "State Bank of India",
    "KOTAKBANK": "Kotak Mahindra Bank",
}


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


def main():
    fetched_at = datetime.now().strftime("%d %b %Y %H:%M IST")
    all_items = []
    feed_errors = {}
    for name, url in FEEDS.items():
        try:
            all_items.extend(_fetch_feed(url))
        except Exception as e:
            feed_errors[name] = str(e)

    position_news = []
    recent_announcements = []
    for item in all_items:
        matched_ticker = next((t for t, name in _POSITION_NAMES.items() if name.lower() in item["company"].lower()), None)
        if matched_ticker:
            position_news.append({**item, "ticker": matched_ticker})
        else:
            recent_announcements.append(item)

    result = {
        "fetched_at": fetched_at,
        "source": "NSE official RSS (nsearchives.nseindia.com) — corporate announcements, not general market news",
        "position_news": position_news[:15],
        "recent_announcements": recent_announcements[:15],
        "feed_errors": feed_errors,
        "tracked_positions": list(_POSITION_NAMES.keys()),
        "disclaimer": "Corporate filings/announcements only (results, board meetings, trading-window closures, etc.), not macro/general market news. General-news API integration deferred — see NEXT UPGRADE in script docstring.",
    }

    OUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"  position_news={len(position_news)} recent={len(recent_announcements)} errors={feed_errors}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
