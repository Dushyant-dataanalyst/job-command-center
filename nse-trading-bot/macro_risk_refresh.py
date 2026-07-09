"""
Macro Risk Overlay -- a systemic "what's happening outside the charts" read
(geopolitics, crude, currency, global markets, VIX) that the 4-voter
technical engine has zero visibility into. Built after a real market flip
wasn't caught because nothing in the pipeline was watching macro drivers.

THIS IS A RISK OVERLAY, NOT A SIGNAL GENERATOR. It never emits a BUY/SELL by
itself -- only risk_score/risk_level/bias plus trade_adjustments (position
sizing, confirmation strictness, sector avoid/watch, blocked tickers) meant
for a FUTURE expert_gate.py wiring to blend with technical_signal +
market_regime + recommendation_history (that wiring is a later step, not
done here -- this module only ships the overlay itself + its JSON feed).

DETERMINISTIC BY DESIGN -- no LLM interpretation of headlines anywhere in
this file. Two kinds of factors, both traceable to a concrete real number or
a real URL+timestamp, never a paraphrase:
  1. Market-data factors (crude, USD/INR, India VIX, US VIX, US indices,
     gold) -- tiered on % change vs previous close. Every such factor
     carries ticker/last/previous/pct_change, thresholds are named
     constants below (grep MARKET-DATA TIERS).
  2. News factors -- keyword-matched against real free sources, never an
     LLM summary:
       - Global geopolitical keywords (war/invasion/escalation/sanctions/
         conflict): GDELT DOC 2.0 API (free, no key) first -- richer,
         many distinct outlets, best for the corroborating-source-count
         logic below. GDELT proved unreliable in practice (08-Jul-2026: hit
         its own "one request per 5s" 429, then separately connect-timed-out
         from a live GitHub Actions run -- a real infra issue, not a local
         block, confirmed from two independent networks -- see CLAUDE.md),
         so BBC World + Al Jazeera RSS (RSS_GEOPOLITICAL_FEEDS, same proven
         technique news_refresh.py already uses for NSE's own RSS) is a
         same-run fallback when GDELT fails outright. Every factor records
         which source actually supplied it (news_source: "gdelt" or
         "rss_fallback"). A single corroborating source is capped
         "rumor"-class and can never alone push a factor into the
         HIGH-risk point range -- see _geopolitical_factor().
       - news_feed.json's own position_news (NSE's official corporate-
         announcement RSS, already fetched by news_refresh.py earlier in
         the same CI run) for fraud/scam/SEBI/probe keywords against
         tickers actually held -- classified "official" (primary source).
       - (added 09-Jul-2026) nseindia.com's own corporate-announcements API
         (undocumented but real -- session-cookie dance verified across 3
         fresh sessions/2 tickers before wiring in) for held tickers' own
         filings, scanned for the same regulatory keywords PLUS auditor/
         director-resignation keywords formal filing language uses that
         news headlines usually don't. Promoter-pledge data specifically
         was NOT found after a time-boxed search -- see data_unavailable.
     A factor with no source URL + timestamp is simply never included.
     News older than NEWS_STALE_HOURS is excluded outright, not just
     down-weighted, so cold news can't stay load-bearing.

NOT BUILT (documented under data_unavailable in the output, never silently
assumed working): FII/DII flows (no free non-scraped source found -- NSE's
own page blocks non-browser requests), Gift Nifty (no stable free source
verified).

RBI MPC / Fed FOMC policy calendars (added 09-Jul-2026): hardcoded, cited,
versioned date constants (RBI_MPC_DATES_FY26_27, FED_FOMC_DATES_2026) --
not a live scrape, these are officially pre-announced and low-volatility.
Carries NO directional bias (a scheduled date says nothing about the
decision itself) -- only tightens confirmation_refreshes/min_votes_required
near the date, surfaced under trade_adjustments + policy_calendar. Degrades
into data_unavailable per-calendar if its hardcoded list goes stale
(>90 days past the last date) rather than silently going empty.

market_regime.json's NIFTY50/BANKNIFTY trend+ADX is surfaced read-only under
regime_context for dashboard/human context -- deliberately NOT re-scored
here, since India VIX (below) already captures the volatility dimension and
double-counting it would be double-dipping the same underlying signal.
"""
import sys, os, json, pathlib, re
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timezone

import time

import pandas as pd
import requests

from ist_time import IST, now_ist, now_ist_str
from yf_retry import download_with_retry

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "macro_risk.json"
NEWS_FEED_FILE = REPO_ROOT / "news_feed.json"
REGIME_FILE = REPO_ROOT / "market_regime.json"

BASELINE_RISK = 15  # markets always carry some baseline uncertainty even on a quiet day

TICKERS = {
    "crude_brent": "BZ=F",
    "usdinr": "INR=X",
    "india_vix": "^INDIAVIX",
    "us_vix": "^VIX",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "gold": "GC=F",
}

# --- MARKET-DATA TIERS --------------------------------------------------
# Each tuple is (threshold, severity, points). A positive threshold matches
# when pct_change >= threshold; a negative threshold matches when
# pct_change <= threshold. Lists are checked in order, first match wins, so
# they must be sorted most-extreme-first. Point ranges are calibrated to
# the brief's own bullets where one exists.
CRUDE_SPIKE_TIERS     = [(5.0, 5, 25), (3.0, 4, 20), (1.5, 2, 15)]     # brief: crude up sharply +15 to +25
CRUDE_RELIEF_TIERS    = [(-3.0, 2, -10)]                                # brief: "oil cooling" reduces risk
USDINR_SPIKE_TIERS    = [(1.5, 5, 20), (0.8, 3, 15), (0.4, 2, 10)]      # brief: USD/INR spike +10 to +20
INDIA_VIX_SPIKE_TIERS = [(20.0, 5, 25), (12.0, 4, 20), (6.0, 2, 15)]    # brief: India VIX spike +15 to +25
INDIA_VIX_RELIEF_TIERS = [(-15.0, 2, -10)]                              # brief: "VIX falling" reduces risk
US_VIX_SPIKE_TIERS    = [(25.0, 4, 15), (15.0, 3, 10), (8.0, 2, 6)]     # supplementary to India VIX, not in brief's bullets -- kept smaller
GOLD_SPIKE_TIERS      = [(3.0, 3, 10), (1.5, 2, 6)]                     # soft safe-haven-flight signal, secondary/supplementary
US_SELLOFF_TIERS      = [(-3.0, 5, 20), (-2.0, 4, 15), (-1.0, 2, 10)]   # brief: US market selloff +10 to +20 (composite S&P/Nasdaq/Dow)
US_RALLY_TIERS        = [(1.5, 2, -8)]                                  # brief: "positive global cues" reduces risk

# --- geopolitical news, GDELT primary + RSS fallback (free, no API key) --
# GDELT is the richer source (searchable, many distinct outlets -- good for
# the corroborating-source-count logic below) but has proven unreliable in
# practice: it 429'd during testing (its OWN documented "one request per 5s"
# limit) and separately connect-timed-out from a live GitHub Actions run
# (08-Jul-2026, see CLAUDE.md) -- a real infra issue, not a local block,
# since both a home ISP and a GH-hosted runner hit it independently. RSS
# feeds from major wire services are the fallback when GDELT fails outright
# -- same proven technique news_refresh.py already uses successfully for
# NSE's own RSS (plain XML + a browser User-Agent, no key, no auth dance).
GEOPOLITICAL_KEYWORDS = ["war", "invasion", "military escalation", "missile strike", "armed conflict", "sanctions"]
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GEOPOLITICAL_QUERY = "(" + " OR ".join(f'"{k}"' for k in GEOPOLITICAL_KEYWORDS) + ") sourcelang:english"
RSS_GEOPOLITICAL_FEEDS = {
    "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "aljazeera_all": "https://www.aljazeera.com/xml/rss/all.xml",
}
NEWS_STALE_HOURS = 48
GEOPOLITICAL_MIN_SOURCES_FOR_HIGH = 3  # single-source hits are capped "rumor" -- can never alone reach this

# --- stock-specific regulatory keywords, scanned against news_feed.json's
# own position_news (NSE official RSS, already fetched this run) ---------
REGULATORY_KEYWORDS = ["fraud", "scam", "probe", "sebi", "penalty", "raid", "insolvency", "default", "fir", "cbi"]

# --- NSE corporate-announcements spike (added 09-Jul-2026, Phase D) -------
# TIME-BOXED SPIKE, GO/NO-GO CRITERION MET: fetched nseindia.com's own
# corporate-announcements API (undocumented but real -- confirmed via 3
# separate fresh sessions across 2 different tickers, all HTTP 200 with real
# dated JSON, not a one-off) -- see nse-trading-bot's git history for the
# exact verification. The interactive-site anti-bot pattern (hit the plain
# HTML page first for session cookies, THEN call the API with them) is a
# HARDER target than news_refresh.py's own NSE fetch (that one hits a static
# XML archive subdomain with just a User-Agent, no cookie dance at all) --
# don't assume this technique is equally durable; if it starts failing,
# _fetch_nse_announcements() returns None per-ticker and the caller degrades
# to whatever news_feed.json-only alerts still work, same fail-open
# discipline as the rest of this module.
#
# NOT FOUND after a reasonable time-boxed effort (5 endpoint-name guesses,
# all 404): a working promoter-pledge-disclosure API. Documented under
# data_unavailable, not silently assumed working -- see DATA_UNAVAILABLE.
NSE_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_ANNOUNCEMENTS_REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
AUDITOR_KEYWORDS = ["resignation of statutory auditor", "resignation of auditor", "auditor resignation",
                     "resignation of director", "removal of director"]

RISK_LEVEL_SIZE_MULT = {"LOW": 1.0, "MODERATE": 0.85, "HIGH": 0.5, "EXTREME": 0.25}
RISK_LEVEL_MIN_VOTES = {"LOW": None, "MODERATE": None, "HIGH": 5, "EXTREME": 6}          # None = defer to expert_gate's own default
RISK_LEVEL_CONFIRM_REFRESHES = {"LOW": None, "MODERATE": None, "HIGH": 3, "EXTREME": 4}  # None = defer to expert_gate's own default

DATA_UNAVAILABLE = {
    "fii_dii_flows": "No free, non-scraped source found -- NSE's FII/DII page blocks non-browser requests. TODO if a reliable API surfaces.",
    "gift_nifty": "No stable free source verified yet.",
    "pledge_disclosure": "NSE's corporate-announcements API works (see stock_specific_alerts' auditor-resignation scan), "
                          "but the promoter-share-pledge endpoint could not be found after 5 time-boxed guesses at its "
                          "path (all 404) -- not the same failure mode as fii_dii_flows/gift_nifty (which have no known "
                          "API at all), this one's endpoint just wasn't located yet.",
}

# --- Scheduled policy calendars (added 09-Jul-2026, Phase D) ---------------
# Hardcoded, cited, versioned date constants -- NOT a live scrape. These are
# officially pre-announced, low-volatility dates (unlike a scraped page they
# can't silently break), so a static list beats adding a new scrape target
# for no real benefit. A scheduled date carries NO directional information
# by itself (the actual decision isn't knowable in advance without
# fabricating a view) -- this only tightens confirmation_refreshes/
# min_votes_required near the date via the same trade_adjustments mechanism
# risk_level already uses, never a bearish/bullish points contribution to
# risk_score. STALENESS GUARD: once the last hardcoded date for a calendar
# is more than POLICY_CALENDAR_STALE_DAYS in the past, that calendar
# degrades into data_unavailable (see main()) rather than silently going
# empty forever -- a reminder to add the next cycle's dates, not a silent gap.
POLICY_CALENDAR_STALE_DAYS = 90
POLICY_WINDOW_DAYS = 2  # tighten within +/- this many calendar days of a policy date

RBI_MPC_SOURCE = "https://www.rbi.org.in/scripts/annualpolicy.aspx"
RBI_MPC_DATES_FY26_27 = [  # RBI's own FY26-27 MPC calendar, announced 23-Mar-2026
    "2026-04-08", "2026-06-05", "2026-08-05", "2026-10-07", "2026-12-04",
]

FED_FOMC_SOURCE = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_FOMC_DATES_2026 = [  # decision day = 2nd day of each 2-day FOMC meeting
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _keyword_hit(text, keywords):
    """First keyword/phrase from `keywords` found in `text` as a whole
    word (regex word-boundary match), or None. NOT a naive substring
    check -- `"war" in text` would false-positive on "warned"/"software"/
    "Delaware" (a real false positive caught 08-Jul-2026: an unrelated US
    Senate-race headline got flagged as "geopolitical escalation" this
    way). Callers pass already-lowercased text; keywords are matched
    case-insensitively regardless. KNOWN LIMITATION: word-boundary matching
    fixes false positives from substrings inside unrelated words (e.g.
    "war" inside "warned"), but a few keywords are still ambiguous even as
    whole words ("invasion" in "invasion of privacy") -- that's an accepted
    tradeoff of deterministic keyword-only matching (no LLM semantic
    understanding, by design); the corroborating-source-count requirement
    in _geopolitical_factor() is the actual backstop against one stray
    ambiguous match driving risk up alone (capped "rumor"-class)."""
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw.strip()) + r"\b", text, re.IGNORECASE):
            return kw.strip()
    return None


def _pct_change(ticker):
    """Last two available daily closes -> (last, prev, pct_change), or all
    None if the fetch fails or returns <2 rows. Deliberately a plain
    yfinance pull (not market_data.get_ohlcv's Kite path) -- this only
    needs two closes for a day-over-day delta, not a signal-grade series."""
    try:
        df = download_with_retry(ticker, period="5d")
        if df.empty or len(df) < 2:
            return None, None, None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        close = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        pct = round((last - prev) / prev * 100, 2) if prev else None
        return round(last, 2), round(prev, 2), pct
    except Exception:
        return None, None, None


def _tier_points(pct, tiers):
    if pct is None:
        return 0, 0
    for threshold, severity, points in tiers:
        if (threshold >= 0 and pct >= threshold) or (threshold < 0 and pct <= threshold):
            return severity, points
    return 0, 0


def _market_factor(name, impact, severity, snap, note):
    return {
        "name": name, "impact": impact, "severity": severity, "evidence": note,
        "source": "market_data", "ticker": snap["ticker"], "last": snap["last"],
        "previous": snap["previous"], "pct_change": snap["pct_change"],
        "evidence_class": "official",  # exchange-sourced price data, not a report/rumor
    }


def _evaluate_ticker(spike_name, relief_name, snap, spike_tiers, relief_tiers=None,
                      spike_impact="bearish", relief_impact="bullish"):
    pct = snap["pct_change"]
    severity, points = _tier_points(pct, spike_tiers)
    if points > 0:
        note = f"{snap['ticker']} moved {pct:+.2f}% vs previous close ({snap['previous']} -> {snap['last']})"
        return points, _market_factor(spike_name, spike_impact, severity, snap, note)
    if relief_tiers:
        severity, points = _tier_points(pct, relief_tiers)
        if points < 0:
            note = f"{snap['ticker']} moved {pct:+.2f}% vs previous close ({snap['previous']} -> {snap['last']})"
            return points, _market_factor(relief_name, relief_impact, severity, snap, note)
    return 0, None


def _us_market_factor(snapshot):
    pcts = {k: snapshot[k]["pct_change"] for k in ("sp500", "nasdaq", "dow") if snapshot[k]["pct_change"] is not None}
    if not pcts:
        return 0, None
    avg_pct = round(sum(pcts.values()) / len(pcts), 2)
    detail = ", ".join(f"{k.upper()} {v:+.2f}%" for k, v in pcts.items())
    severity, points = _tier_points(avg_pct, US_SELLOFF_TIERS)
    if points > 0:
        return points, {
            "name": "US markets selloff (last close)", "impact": "bearish", "severity": severity,
            "evidence": f"Composite avg {avg_pct:+.2f}% across S&P500/Nasdaq/Dow last close vs previous session ({detail})",
            "source": "market_data", "pct_change": avg_pct, "evidence_class": "official",
        }
    severity, points = _tier_points(avg_pct, US_RALLY_TIERS)
    if points < 0:
        return points, {
            "name": "US markets rallying (last close)", "impact": "bullish", "severity": severity,
            "evidence": f"Composite avg {avg_pct:+.2f}% across S&P500/Nasdaq/Dow last close vs previous session ({detail})",
            "source": "market_data", "pct_change": avg_pct, "evidence_class": "official",
        }
    return 0, None


def _parse_gdelt_date(s):
    try:
        dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(IST)
    except Exception:
        return None


def _fetch_gdelt_articles():
    """GDELT-only fetch. Returns (articles, error_detail): articles is a
    list of fresh (<= NEWS_STALE_HOURS old) matching articles, or None if
    the request itself failed -- None is deliberately distinct from []
    (fetched fine, nothing matched) so the caller can report an honest
    'fetch_failed' vs 'no_matches' status rather than conflating the two
    into a fake zero. error_detail carries the actual exception/status so a
    'fetch_failed' in macro_risk.json is diagnosable from the committed
    JSON alone, without needing GitHub Actions log access. GDELT is a free
    public API with no uptime SLA -- one retry on a transient failure, then
    give up cleanly (see _fetch_geopolitical_articles() for the RSS
    fallback this feeds into)."""
    articles = None
    last_error = None
    for attempt in range(2):
        if attempt > 0:
            # GDELT's own published limit is "one request per 5 seconds" --
            # retrying immediately on failure would violate that on our OWN
            # request, not just risk hitting someone else's. 8s comfortably
            # clears it. (A 429 witnessed 08-Jul-2026 during manual testing
            # traced to this -- the retry had no backoff at all.)
            time.sleep(8)
        try:
            resp = requests.get(GDELT_URL, params={
                "query": GEOPOLITICAL_QUERY, "mode": "artlist", "format": "json",
                "maxrecords": 20, "sort": "datedesc", "timespan": "2days",
            }, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
                if attempt == 1:
                    return None, last_error
                continue
            try:
                articles = resp.json().get("articles", [])
            except ValueError:
                # GDELT returns HTTP 200 with a plain-text/HTML error page
                # (not JSON) when it rejects a query's syntax -- this is a
                # DIFFERENT failure mode than an unreachable host, and would
                # otherwise be silently indistinguishable from a timeout.
                last_error = f"non-JSON 200 response (likely query rejected): {resp.text[:200]!r}"
                if attempt == 1:
                    return None, last_error
                continue
            last_error = None
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt == 1:
                return None, last_error

    now = now_ist()
    fresh = []
    for a in articles:
        pub = _parse_gdelt_date(a.get("seendate", ""))
        if pub is None:
            continue
        age_hours = (now - pub).total_seconds() / 3600
        if age_hours > NEWS_STALE_HOURS:
            continue
        fresh.append({
            "title": a.get("title"), "url": a.get("url"), "domain": a.get("domain"),
            "published_at": pub.strftime("%d %b %Y %H:%M IST"), "age_hours": round(age_hours, 1),
        })
    return fresh, None


def _fetch_rss_geopolitical_articles():
    """Fallback when GDELT fails outright -- scans RSS_GEOPOLITICAL_FEEDS
    (major wire services, no key/auth) for GEOPOLITICAL_KEYWORDS in each
    item's title+description. Same proven technique news_refresh.py already
    uses successfully for NSE's own RSS (plain XML + browser User-Agent).
    One dead feed doesn't kill the other -- only fails (returns None) if
    EVERY configured feed fails. Returns (articles_or_None, error_detail)."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime

    now = now_ist()
    fresh = []
    feed_errors = {}
    for domain, url in RSS_GEOPOLITICAL_FEEDS.items():
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
        except Exception as e:
            feed_errors[domain] = f"{type(e).__name__}: {e}"
            continue
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_raw = (item.findtext("pubDate") or "").strip()
            text = f"{title} {desc}".lower()
            if not _keyword_hit(text, GEOPOLITICAL_KEYWORDS):
                continue
            try:
                pub = parsedate_to_datetime(pub_raw)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
                pub = pub.astimezone(IST)
            except Exception:
                continue
            age_hours = (now - pub).total_seconds() / 3600
            if age_hours > NEWS_STALE_HOURS:
                continue
            fresh.append({
                "title": title, "url": link, "domain": domain,
                "published_at": pub.strftime("%d %b %Y %H:%M IST"), "age_hours": round(age_hours, 1),
            })

    if len(feed_errors) == len(RSS_GEOPOLITICAL_FEEDS):
        return None, f"all RSS feeds failed: {feed_errors}"
    return fresh, None


def _fetch_geopolitical_articles():
    """Orchestrator: GDELT first (richer -- searchable, many distinct
    outlets, better for the corroborating-source-count logic below), RSS as
    fallback only if GDELT fails outright. Returns (articles_or_None,
    source_used, error_detail) -- error_detail is only set when BOTH
    sources fail."""
    articles, gdelt_error = _fetch_gdelt_articles()
    if articles is not None:
        return articles, "gdelt", None
    rss_articles, rss_error = _fetch_rss_geopolitical_articles()
    if rss_articles is not None:
        return rss_articles, "rss_fallback", None
    return None, None, f"gdelt: {gdelt_error} | rss_fallback: {rss_error}"


def _geopolitical_factor(articles, fetch_error, news_source):
    """articles is None (both GDELT and the RSS fallback failed -- see
    fetch_error), [] (fetched fine, no matches), or a list of fresh
    matches. news_source is 'gdelt' or 'rss_fallback', for transparency on
    which path actually supplied the evidence. Severity/points scale with
    the number of DISTINCT corroborating source domains, never a single
    headline's wording -- a lone unconfirmed report is capped 'rumor'-class
    (severity 2, 12 points) and can never alone reach the HIGH-risk point
    range, per the brief's anti-hallucination rule."""
    if not articles:
        status = f"fetch_failed: {fetch_error}" if articles is None else f"no_matches ({news_source})"
        return 0, None, status

    domains = sorted({a["domain"] for a in articles if a.get("domain")})
    n_domains = len(domains) or 1
    top = articles[0]

    if n_domains >= GEOPOLITICAL_MIN_SOURCES_FOR_HIGH:
        severity, points, cls = 5, 30, "reported"
    elif n_domains >= 2:
        severity, points, cls = 3, 20, "reported"
    else:
        severity, points, cls = 2, 12, "rumor"

    factor = {
        "name": "Geopolitical escalation", "impact": "bearish", "severity": severity,
        "evidence": f"{len(articles)} article(s) from {n_domains} distinct source(s) matching war/escalation/"
                    f"conflict keywords in the last {NEWS_STALE_HOURS}h -- most recent: \"{top['title']}\"",
        "source": top["url"], "published_at": top["published_at"], "evidence_class": cls,
        "corroborating_sources": n_domains, "news_source": news_source,
    }
    return points, factor, f"ok ({news_source})"


def _fetch_nse_announcements(symbol):
    """One held ticker's recent NSE corporate announcements, or None on ANY
    fetch/parse problem (fail open -- a broken fetch for one ticker never
    poisons the whole scan, see _nse_announcement_alerts()). Session dance
    (hit the plain page first for anti-bot cookies, then call the API) is a
    harder target than news_refresh.py's static-XML-archive fetch -- see the
    module-level comment above NSE_ANNOUNCEMENTS_URL."""
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": NSE_ANNOUNCEMENTS_REFERER,
        })
        r1 = s.get(NSE_ANNOUNCEMENTS_REFERER, timeout=15)
        if r1.status_code != 200:
            return None
        r2 = s.get(NSE_ANNOUNCEMENTS_URL, params={"index": "equities", "symbol": symbol}, timeout=15)
        if r2.status_code != 200:
            return None
        data = r2.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _parse_nse_announcement_dt(s):
    """'08-Jul-2026 19:08:15' -> IST-aware datetime, or None."""
    try:
        return datetime.strptime(s, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
    except Exception:
        return None


def _nse_announcement_alerts(held_tickers):
    """Scans each held ticker's recent (<=NEWS_STALE_HOURS old) NSE
    corporate announcements for regulatory + auditor/director-resignation
    keywords. Held tickers ONLY (not the full universe) -- same scope
    discipline as the news_feed.json-based scan below, and the same reason:
    bounding live network calls to what's actually load-bearing. One
    ticker's fetch failure doesn't affect the others."""
    now = now_ist()
    alerts = []
    fetch_errors = {}
    for symbol in held_tickers:
        items = _fetch_nse_announcements(symbol)
        if items is None:
            fetch_errors[symbol] = "fetch_failed"
            continue
        for item in items:
            pub = _parse_nse_announcement_dt(item.get("an_dt", ""))
            if pub is None or (now - pub).total_seconds() / 3600 > NEWS_STALE_HOURS:
                continue
            text = (item.get("attchmntText") or item.get("desc") or "").lower()
            hit = _keyword_hit(text, REGULATORY_KEYWORDS) or _keyword_hit(text, AUDITOR_KEYWORDS)
            if hit:
                alerts.append({
                    "ticker": symbol, "matched_keyword": hit.strip(),
                    "headline": (item.get("attchmntText") or "")[:200],
                    "source": item.get("attchmntFile"), "published_at": pub.strftime("%d %b %Y %H:%M IST"),
                    "evidence_class": "official", "news_source": "nse_corporate_announcements",
                })
    return alerts, fetch_errors


def _regulatory_alerts():
    """Scans news_feed.json's own position_news (NSE's official
    announcement RSS, already fetched this run by news_refresh.py) for
    regulatory-risk keywords against tickers actually held, PLUS (added
    09-Jul-2026) each held ticker's own NSE corporate-announcements feed for
    the same regulatory keywords + auditor/director-resignation keywords
    that news RSS alone wouldn't reliably surface (formal filing language
    differs from news-headline language). Classified 'official' either
    way -- both are primary NSE sources, not secondhand reports."""
    data = _load_json(NEWS_FEED_FILE, {})
    alerts = []
    for item in data.get("position_news", []):
        if item.get("kind") != "holding":
            continue
        text = f"{item.get('company', '')} {item.get('summary', '')}".lower()
        hit = _keyword_hit(text, REGULATORY_KEYWORDS)
        if hit:
            alerts.append({
                "ticker": item.get("ticker"), "matched_keyword": hit.strip(),
                "headline": item.get("company"), "source": item.get("link"),
                "published_at": item.get("published"), "evidence_class": "official",
                "news_source": "nse_rss",
            })

    held_tickers = data.get("tracked_positions", [])
    nse_alerts, nse_fetch_errors = _nse_announcement_alerts(held_tickers) if held_tickers else ([], {})
    alerts.extend(nse_alerts)
    return alerts, nse_fetch_errors


def _risk_level(score):
    if score <= 30: return "LOW"
    if score <= 60: return "MODERATE"
    if score <= 80: return "HIGH"
    return "EXTREME"


def _max_optional(*vals):
    """max() over whatever isn't None, or None if nothing is -- lets a
    caller combine two independent "escalate if set" sources (risk_level's
    own value + a policy-calendar override) without either one silently
    looking like a real 0/None when the other actually has a value."""
    real = [v for v in vals if v is not None]
    return max(real) if real else None


def _policy_calendar_context(now_dt):
    """Returns (policy_calendar_dict, escalation_dict). escalation_dict
    carries min_votes_required/confirmation_refreshes ONLY when today falls
    within POLICY_WINDOW_DAYS of a real hardcoded RBI/Fed date -- empty
    otherwise, so main() just defers entirely to risk_level's own values
    when nothing is upcoming. Never touches risk_score/bias -- see the
    module-level comment above the date constants for why."""
    today = now_dt.date()
    stale = {}
    upcoming = []

    for label, dates, source in (("RBI MPC", RBI_MPC_DATES_FY26_27, RBI_MPC_SOURCE),
                                  ("Fed FOMC", FED_FOMC_DATES_2026, FED_FOMC_SOURCE)):
        parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]
        last_date = max(parsed)
        if (today - last_date).days > POLICY_CALENDAR_STALE_DAYS:
            stale[label] = (f"last hardcoded date ({last_date}) is more than {POLICY_CALENDAR_STALE_DAYS}d in the "
                             f"past -- needs the next cycle's dates added. Source: {source}")
            continue
        for d in parsed:
            days_away = (d - today).days
            if abs(days_away) <= POLICY_WINDOW_DAYS:
                upcoming.append({"event": label, "date": str(d), "days_away": days_away, "source": source})

    upcoming.sort(key=lambda x: abs(x["days_away"]))
    escalation = {"min_votes_required": 5, "confirmation_refreshes": 3} if upcoming else {}
    policy_calendar = {
        "window_days": POLICY_WINDOW_DAYS,
        "upcoming_events": upcoming,
        "escalation_applied": bool(upcoming),
        "note": "A scheduled policy date carries no directional bias by itself -- only tightens confirmation "
                "strictness near it, same as a HIGH/EXTREME macro risk_level would.",
    }
    return policy_calendar, escalation, stale


def _regime_context():
    regime = _load_json(REGIME_FILE, {})
    inst = regime.get("instruments", {}) if isinstance(regime, dict) else {}
    return {name: {"trend": r.get("trend"), "adx": r.get("adx"), "volatility": r.get("volatility")}
            for name, r in inst.items()}


def main():
    fetched_at = now_ist_str()
    factors = []
    net_points = 0

    market_snapshot = {}
    for key, ticker in TICKERS.items():
        last, prev, pct = _pct_change(ticker)
        market_snapshot[key] = {"ticker": ticker, "last": last, "previous": prev, "pct_change": pct}

    for pts, f in [
        _evaluate_ticker("Crude oil spike", "Crude oil cooling", market_snapshot["crude_brent"], CRUDE_SPIKE_TIERS, CRUDE_RELIEF_TIERS),
        _evaluate_ticker("USD/INR spike", "USD/INR easing", market_snapshot["usdinr"], USDINR_SPIKE_TIERS),
        _evaluate_ticker("India VIX spike", "India VIX falling", market_snapshot["india_vix"], INDIA_VIX_SPIKE_TIERS, INDIA_VIX_RELIEF_TIERS),
        _evaluate_ticker("US VIX spike", "US VIX falling", market_snapshot["us_vix"], US_VIX_SPIKE_TIERS),
        _evaluate_ticker("Gold safe-haven spike", "Gold easing", market_snapshot["gold"], GOLD_SPIKE_TIERS),
        _us_market_factor(market_snapshot),
    ]:
        if f:
            factors.append(f)
            net_points += pts

    geo_articles, geo_source, geo_fetch_error = _fetch_geopolitical_articles()
    geo_points, geo_factor, geo_status = _geopolitical_factor(geo_articles, geo_fetch_error, geo_source)
    if geo_factor:
        factors.append(geo_factor)
        net_points += geo_points

    regulatory_alerts, nse_announcement_fetch_errors = _regulatory_alerts()
    policy_calendar, policy_escalation, policy_stale = _policy_calendar_context(now_ist())

    risk_score = max(0, min(100, round(BASELINE_RISK + net_points)))
    risk_level = _risk_level(risk_score)
    bias = "BEARISH" if net_points > 10 else "BULLISH" if net_points < -10 else "NEUTRAL"

    successful = sum(1 for v in market_snapshot.values() if v["pct_change"] is not None)
    confidence = 0.5 + 0.4 * (successful / len(market_snapshot))
    if any(f.get("evidence_class") == "rumor" for f in factors):
        confidence -= 0.15
    confidence = round(max(0.2, min(0.95, confidence)), 2)

    factor_names = {f["name"] for f in factors}
    avoid_sectors, watch_sectors = set(), set()
    if "Crude oil spike" in factor_names:
        avoid_sectors |= {"Auto", "Aviation", "OMC"}
        watch_sectors |= {"Energy"}
    if "Geopolitical escalation" in factor_names:
        watch_sectors |= {"Energy", "Defence"}
    if "USD/INR spike" in factor_names:
        watch_sectors |= {"IT", "Pharma"}  # export-oriented, benefit from rupee weakness

    allow_new_longs = not (bias == "BEARISH" and risk_level in ("HIGH", "EXTREME"))
    allow_new_shorts = not (bias == "BULLISH" and risk_level in ("HIGH", "EXTREME"))

    trade_adjustments = {
        "allow_new_longs": allow_new_longs,
        "allow_new_shorts": allow_new_shorts,
        # _max_optional combines risk_level's own escalation with the policy-
        # calendar one (added 09-Jul-2026) -- whichever is stricter wins,
        # same "can only ever tighten, never loosen" contract macro_gate.
        # escalated() already applies downstream. Stays None (defer to
        # expert_gate's own default) only when BOTH are None.
        "min_votes_required": _max_optional(RISK_LEVEL_MIN_VOTES[risk_level], policy_escalation.get("min_votes_required")),
        "confirmation_refreshes": _max_optional(RISK_LEVEL_CONFIRM_REFRESHES[risk_level], policy_escalation.get("confirmation_refreshes")),
        "position_size_multiplier": RISK_LEVEL_SIZE_MULT[risk_level],
        "avoid_sectors": sorted(avoid_sectors),
        "watch_sectors": sorted(watch_sectors - avoid_sectors),
        "blocked_tickers": sorted({a["ticker"] for a in regulatory_alerts if a.get("ticker")}),
    }

    data_unavailable = {**DATA_UNAVAILABLE, **policy_stale}

    result = {
        "fetched_at": fetched_at,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "bias": bias,
        "confidence": confidence,
        "factors": factors,
        "stock_specific_alerts": regulatory_alerts,
        "nse_announcement_fetch_errors": nse_announcement_fetch_errors,
        "trade_adjustments": trade_adjustments,
        "policy_calendar": policy_calendar,
        "market_snapshot": market_snapshot,
        "regime_context": _regime_context(),
        "geopolitical_feed_status": geo_status,
        "data_unavailable": data_unavailable,
        "method": f"risk_score = baseline({BASELINE_RISK}) + sum of factor points, clamped 0-100. "
                  "Market-data factors are tiered on % move vs previous close (thresholds are named constants "
                  "in source, see MARKET-DATA TIERS). News factors require a real source URL + timestamp -- no "
                  "source, no factor; news older than 48h is excluded outright. Geopolitical severity is capped "
                  "when only one source domain corroborates it (rumor-tier, cannot alone reach HIGH). "
                  "bias = BEARISH/BULLISH/NEUTRAL by sign of net points (>+10 / <-10 / between). confidence = "
                  "0.5 + 0.4*(market-data fetch completeness), shaved 0.15 if any factor is rumor-class.",
        "disclaimer": "Macro risk overlay only. Does NOT generate trades by itself and does not replace the "
                      "technical signal engine -- it is a risk-adjustment layer for the future expert gate to "
                      "blend with technical_signal + market_regime + recommendation_history. Deterministic "
                      "scoring only, no LLM interpretation of headlines anywhere in this module. Educational "
                      "use only, not investment advice.",
    }

    OUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  risk_score={risk_score} ({risk_level}) bias={bias} confidence={confidence}")
    print(f"  factors={len(factors)} stock_alerts={len(regulatory_alerts)} geo_feed={geo_status}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        fetched_at = now_ist_str()
        OUT_FILE.write_text(json.dumps({
            "error": str(e), "fetched_at": fetched_at, "risk_score": None,
            "risk_level": "UNKNOWN", "bias": "UNKNOWN", "confidence": 0.0,
            "factors": [], "disclaimer": "Macro risk overlay only. Refresh failed this run -- see 'error'.",
        }, indent=2), encoding="utf-8")
        print(f"  ERROR in main(): {e} -- wrote error-state JSON")
