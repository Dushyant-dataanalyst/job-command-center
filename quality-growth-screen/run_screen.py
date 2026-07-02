"""
System B entrypoint — Master Brief Part 4. Run manually (`python run_screen.py`)
whenever you want a refreshed ranked watchlist; this is a research tool, not
a live feed, so it isn't wired into any cron by default.

Writes quality_growth_watchlist.json in this folder — separate from every
JSON file the trading bot's dashboard reads, on purpose.
"""
import sys, os, json, pathlib, time
sys.path.insert(0, os.path.dirname(__file__))

import config
from data_fetch import fetch_stock_data
from screen import score_stock

OUT_FILE = pathlib.Path(__file__).parent / config.OUTPUT_FILE


def main():
    results = []
    for i, ticker in enumerate(config.WATCHLIST):
        print(f"  [{i+1}/{len(config.WATCHLIST)}] {ticker} ...")
        data = fetch_stock_data(ticker)
        result = score_stock(ticker, data)
        results.append(result)
        if "error" in data:
            print(f"    ERROR: {data['error']}")
        else:
            print(f"    composite {result['composite']}/100" + (f" -- {result['hard_fail']}" if result['hard_fail'] else ""))
        time.sleep(1)  # be polite to yfinance between tickers

    ranked = sorted(
        [r for r in results if r["composite"] is not None],
        key=lambda r: r["composite"],
        reverse=True,
    )
    failed = [r for r in results if r["composite"] is None]

    output = {
        "generated_at": time.strftime("%d %b %Y %H:%M"),
        "disclaimer": "Educational long-term research screen only, not investment advice. "
                      "Composite score is a heuristic weighting (quality 35 / growth 30 / "
                      "valuation 20 / india red-flags 15), not a verified backtest of what "
                      "predicts future returns. Never places orders.",
        "ranked": ranked,
        "fetch_failed": failed,
    }
    OUT_FILE.write_text(json.dumps(output, indent=2), encoding="utf-8")

    print(f"\n  Wrote {OUT_FILE}")
    print("\n  Ranked (highest composite first):")
    for r in ranked:
        flag = f"  [{r['hard_fail']}]" if r["hard_fail"] else ""
        print(f"    {r['composite']:>5.1f}  {r['ticker']}{flag}")
        print(f"           {r['summary']}")
    if failed:
        print(f"\n  {len(failed)} ticker(s) failed to fetch: {[r['ticker'] for r in failed]}")


if __name__ == "__main__":
    main()
