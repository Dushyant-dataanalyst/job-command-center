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
  2. News factors -- keyword-matched against two real free sources, never
     an LLM summary:
       - GDELT DOC 2.0 API (free, no key) for global geopolitical keywords
         (war/invasion/escalation/sanctions/conflict). A single corroborating
         source is capped "rumor"-class and can never alone push a factor
         into the HIGH-risk point range -- see _geopolitical_factor().
       - news_feed.json's own position_news (NSE's official corporate-
         announcement RSS, already fetched by news_refresh.py earlier in
         the same CI run) for fraud/scam/SEBI/probe keywords against
         tickers actually held -- classified "official" (primary source).
     A factor with no source URL + timestamp is simply never included.
     News older than NEWS_STALE_HOURS is excluded outright, not just
     down-weighted, so cold news can't stay load-bearing.

NOT BUILT (documented under data_unavailable in the output, never silently
assumed working): FII/DII flows (no free non-scraped source found -- NSE's
own page blocks non-browser requests), Gift Nifty (no stable free source
verified), RBI/Fed calendar (explicitly "later" per the build brief).

market_regime.json's NIFTY50/BANKNIFTY trend+ADX is surfaced read-only under
regime_context for dashboard/human context -- deliberately NOT re-scored
here, since India VIX (below) already captures the volatility dimension and
double-counting it would be double-dipping the same underlying signal.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime, timezone

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

# --- GDELT (free, no API key) -------------------------------------------
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GEOPOLITICAL_QUERY = '("war" OR "invasion" OR "military escalation" OR "missile strike" OR "armed conflict" OR "sanctions") sourcelang:english'
NEWS_STALE_HOURS = 48
GEOPOLITICAL_MIN_SOURCES_FOR_HIGH = 3  # single-source hits are capped "rumor" -- can never alone reach this

# --- stock-specific regulatory keywords, scanned against news_feed.json's
# own position_news (NSE official RSS, already fetched this run) ---------
REGULATORY_KEYWORDS = ["fraud", "scam", "probe", "sebi", "penalty", "raid", "insolvency", "default", "fir ", "cbi "]

RISK_LEVEL_SIZE_MULT = {"LOW": 1.0, "MODERATE": 0.85, "HIGH": 0.5, "EXTREME": 0.25}
RISK_LEVEL_MIN_VOTES = {"LOW": None, "MODERATE": None, "HIGH": 5, "EXTREME": 6}          # None = defer to expert_gate's own default
RISK_LEVEL_CONFIRM_REFRESHES = {"LOW": None, "MODERATE": None, "HIGH": 3, "EXTREME": 4}  # None = defer to expert_gate's own default

DATA_UNAVAILABLE = {
    "fii_dii_flows": "No free, non-scraped source found -- NSE's FII/DII page blocks non-browser requests. TODO if a reliable API surfaces.",
    "gift_nifty": "No stable free source verified yet.",
    "rbi_fed_calendar": "Deferred per build brief ('later').",
}


def _load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


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


def _fetch_geopolitical_articles():
    """Returns a list of fresh (<= NEWS_STALE_HOURS old) matching articles,
    or None if the GDELT request itself failed -- None is deliberately
    distinct from [] (fetched fine, nothing matched) so the caller can
    report an honest 'fetch_failed' vs 'no_matches' status rather than
    conflating the two into a fake zero. GDELT is a free public API with no
    uptime SLA -- one retry on a transient failure, then give up cleanly."""
    articles = None
    for attempt in range(2):
        try:
            resp = requests.get(GDELT_URL, params={
                "query": GEOPOLITICAL_QUERY, "mode": "artlist", "format": "json",
                "maxrecords": 20, "sort": "datedesc", "timespan": "2days",
            }, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
            articles = resp.json().get("articles", [])
            break
        except Exception:
            if attempt == 1:
                return None

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
    return fresh


def _geopolitical_factor(articles):
    """articles is None (fetch failed), [] (fetched, no matches), or a list
    of fresh matches. Severity/points scale with the number of DISTINCT
    corroborating source domains, never a single headline's wording -- a
    lone unconfirmed report is capped 'rumor'-class (severity 2, 12 points)
    and can never alone reach the HIGH-risk point range, per the brief's
    anti-hallucination rule."""
    if not articles:
        status = "fetch_failed" if articles is None else "no_matches"
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
        "corroborating_sources": n_domains,
    }
    return points, factor, "ok"


def _regulatory_alerts():
    """Scans news_feed.json's own position_news (NSE's official
    announcement RSS, already fetched this run by news_refresh.py) for
    regulatory-risk keywords against tickers actually held. Classified
    'official' -- this is the primary NSE feed, not a secondhand report."""
    data = _load_json(NEWS_FEED_FILE, {})
    alerts = []
    for item in data.get("position_news", []):
        if item.get("kind") != "holding":
            continue
        text = f"{item.get('company', '')} {item.get('summary', '')}".lower()
        hit = next((kw for kw in REGULATORY_KEYWORDS if kw in text), None)
        if hit:
            alerts.append({
                "ticker": item.get("ticker"), "matched_keyword": hit.strip(),
                "headline": item.get("company"), "source": item.get("link"),
                "published_at": item.get("published"), "evidence_class": "official",
            })
    return alerts


def _risk_level(score):
    if score <= 30: return "LOW"
    if score <= 60: return "MODERATE"
    if score <= 80: return "HIGH"
    return "EXTREME"


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

    geo_articles = _fetch_geopolitical_articles()
    geo_points, geo_factor, geo_status = _geopolitical_factor(geo_articles)
    if geo_factor:
        factors.append(geo_factor)
        net_points += geo_points

    regulatory_alerts = _regulatory_alerts()

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
        "min_votes_required": RISK_LEVEL_MIN_VOTES[risk_level],
        "confirmation_refreshes": RISK_LEVEL_CONFIRM_REFRESHES[risk_level],
        "position_size_multiplier": RISK_LEVEL_SIZE_MULT[risk_level],
        "avoid_sectors": sorted(avoid_sectors),
        "watch_sectors": sorted(watch_sectors - avoid_sectors),
        "blocked_tickers": sorted({a["ticker"] for a in regulatory_alerts if a.get("ticker")}),
    }

    result = {
        "fetched_at": fetched_at,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "bias": bias,
        "confidence": confidence,
        "factors": factors,
        "stock_specific_alerts": regulatory_alerts,
        "trade_adjustments": trade_adjustments,
        "market_snapshot": market_snapshot,
        "regime_context": _regime_context(),
        "geopolitical_feed_status": geo_status,
        "data_unavailable": DATA_UNAVAILABLE,
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
