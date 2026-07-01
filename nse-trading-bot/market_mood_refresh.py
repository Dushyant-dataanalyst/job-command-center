"""
Market Mood — a transparent, constructed Fear/Greed proxy for NSE.

No free official India Fear & Greed index exists, so this builds one from
three live, verifiable components (documented, not a black box):

  1. India VIX (40%)        — ^INDIAVIX via yfinance. Lower VIX = calmer
                               market = higher score.
  2. Market breadth (30%)   — % of a 50-stock large-cap basket (the same
                               verified SECTOR_STOCKS basket used by
                               sector_rotation_core.py) trading above their
                               20-day EMA.
  3. NIFTY momentum (30%)   — EMA9/18/50 alignment + 5-day ROC on NIFTY50,
                               reusing the same indicator logic as
                               refresh_fo_cloud.py's _get_indicators().

NOTE: this deviates slightly from an earlier plan draft that assumed the
dashboard's hand-embedded SCAN_DATA could be reused for breadth — it can't,
because no Python script in this repo generates SCAN_DATA (it's pasted into
the HTML by a separate process). Breadth here is computed fresh from a
verified-working stock basket instead, kept transparent in the output.

Output is a 0-100 composite score + band label + the three raw components,
so nothing is hidden — every number here is fetched live at call time.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

from refresh_fo_cloud import _get_indicators
from sector_rotation_core import SECTOR_STOCKS
from yf_retry import download_with_retry

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "market_mood.json"

VIX_TICKER = "^INDIAVIX"
NIFTY_TICKER = "^NSEI"
VIX_CALM = 10.0   # India VIX floor for "very calm" — score 100
VIX_FEAR = 30.0   # India VIX ceiling for "extreme fear" — score 0


def _vix_score():
    df = download_with_retry(VIX_TICKER, period="5d")
    if df.empty:
        return None, None
    level = float(df["Close"].iloc[-1]) if "Close" in df.columns else float(df.iloc[-1, 0])
    score = (VIX_FEAR - level) / (VIX_FEAR - VIX_CALM) * 100
    return round(max(0, min(100, score))), round(level, 2)


def _breadth_score():
    tickers = [t for sector_list in SECTOR_STOCKS.values() for t in sector_list]
    above, total = 0, 0
    for t in tickers:
        try:
            df = download_with_retry(t, period="2mo")
            if df.empty or len(df) < 21:
                continue
            close = df["Close"] if "Close" in df.columns else df.iloc[:, 0]
            ema20 = close.ewm(span=20, adjust=False).mean()
            total += 1
            if float(close.iloc[-1]) > float(ema20.iloc[-1]):
                above += 1
        except Exception:
            continue
    if total == 0:
        return None, None
    pct = round(above / total * 100)
    return pct, f"{above}/{total} stocks above 20-day EMA"


def _momentum_score():
    v = _get_indicators(NIFTY_TICKER)
    if not v:
        return None, None
    base = 50
    if v["spot"] > v["ema18"] > v["ema50"]:
        base = 65
    elif v["spot"] < v["ema18"] < v["ema50"]:
        base = 35
    nudge = max(-20, min(20, v["roc5"] * 4))
    score = round(max(0, min(100, base + nudge)))
    return score, {"roc5_pct": v["roc5"], "ema_aligned_bullish": v["spot"] > v["ema18"] > v["ema50"],
                    "ema_aligned_bearish": v["spot"] < v["ema18"] < v["ema50"], "data_as_of": v["data_date"]}


def _band(score):
    if score < 20: return "Extreme Fear"
    if score < 40: return "Fear"
    if score < 60: return "Neutral"
    if score < 80: return "Greed"
    return "Extreme Greed"


def main():
    try:
        fetched_at = datetime.now().strftime("%d %b %Y %H:%M IST")
        vix_score, vix_level = _vix_score()
        breadth_score, breadth_detail = _breadth_score()
        momentum_score, momentum_detail = _momentum_score()

        components = {
            "india_vix": {"score": vix_score, "weight_pct": 40, "level": vix_level},
            "market_breadth": {"score": breadth_score, "weight_pct": 30, "detail": breadth_detail},
            "nifty_momentum": {"score": momentum_score, "weight_pct": 30, "detail": momentum_detail},
        }

        valid = [(c["score"], c["weight_pct"]) for c in components.values() if c["score"] is not None]
        if valid:
            total_weight = sum(w for _, w in valid)
            composite = round(sum(s * w for s, w in valid) / total_weight)
        else:
            composite = None

        result = {
            "fetched_at": fetched_at,
            "composite_score": composite,
            "label": _band(composite) if composite is not None else "No data",
            "components": components,
            "method": "composite = weighted avg of (40% India VIX inverted, 30% market breadth % above 20-day EMA across a 50-stock basket, 30% NIFTY EMA-alignment + ROC5 momentum). Missing components are excluded and weights renormalized, not faked.",
            "disclaimer": "Constructed proxy index, not an official Fear & Greed index (none exists free for India). Educational use only.",
        }

        OUT_FILE.write_text(json.dumps(result, indent=2))
        print(f"  composite={composite} ({result['label']}) vix={vix_level} breadth={breadth_score}% momentum={momentum_score}")
        print(f"  Wrote {OUT_FILE}")
    except Exception as e:
        fetched_at = datetime.now().strftime("%d %b %Y %H:%M IST")
        OUT_FILE.write_text(json.dumps({
            "error": str(e),
            "fetched_at": fetched_at,
            "composite_score": None,
            "label": "No data",
        }, indent=2))
        print(f"  ERROR in main(): {e} — wrote error-state JSON")


if __name__ == "__main__":
    main()
