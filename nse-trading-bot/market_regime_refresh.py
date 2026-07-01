"""
Market regime refresh — writes market_regime.json to the repo root so the
dashboard can fetch it. Runs on the same cron as the F&O refresh (only 2
tickers, cheap enough for the 5-min market-hours cadence).
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

from market_regime_core import detect_regime

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "market_regime.json"


def main():
    try:
        now_str = datetime.now().strftime("%d %b %Y %H:%M IST")
        result = detect_regime()
        result["fetched_at"] = now_str
        OUT_FILE.write_text(json.dumps(result, indent=2))
        for name, r in result["instruments"].items():
            print(f"  {name}: {r['trend']} | volatility={r['volatility']} ({r['atr_pct']}%, {r['atr_percentile_6mo']}th pct) | volume={r['volume_behavior']}")
        print(f"  Best fit: {result['recommendation']['best_fit_strategies']} | Avoid: {result['recommendation']['avoid']}")
        print(f"  Wrote {OUT_FILE}")
    except Exception as e:
        OUT_FILE.write_text(json.dumps({
            "fetched_at": datetime.now().strftime("%d %b %Y %H:%M IST"),
            "error": str(e),
            "instruments": {},
            "recommendation": {"best_fit_strategies": [], "avoid": [], "reasoning": []},
        }, indent=2))
        print(f"  ERROR in main(): {e} — wrote error-state JSON")


if __name__ == "__main__":
    main()
