"""
Guards against Python/JS premium-formula drift — the exact bug class that
misprized a real position on 06-Jul-2026 (NIFTY50 24300 CE showed -9% on the
dashboard while genuinely +28%): the option-premium model exists in TWO
places that MUST stay identical:

  1. Python: refresh_fo_cloud.py::_premium_estimate()  (signal engine,
     trade_brain paper trades, stock_fo suggestions)
  2. JS:     nse_live_dashboard.html::_estimatePremium() (My F&O Positions
     panel re-pricing the user's real trades client-side)

On 04-Jul-2026 the Python side was fixed (intrinsic-value floor added) but
the JS twin was forgotten, so for two days the dashboard showed fake losses
on winning ITM positions. This script makes that impossible to repeat
silently: it extracts the ACTUAL JS function from the HTML, executes it in
Node, runs the ACTUAL Python function on the same input grid, and fails
(exit 1) if any case diverges by more than 1 rupee.

Why tolerance of 1 and not 0: Python's round() is banker's rounding
(half-to-even), JS Math.round is half-up — on an exact .5 they legitimately
differ by 1. Anything beyond that is real formula drift.

Also asserts the property the original bug violated, in BOTH languages:
an option's premium can never be below its intrinsic value.

Run:  cd nse-trading-bot && python check_premium_parity.py
CI:   .github/workflows/premium_parity.yml (fires on push when either
      implementation file changes)
Requires node on PATH (preinstalled on ubuntu-latest; present locally).
"""
import sys, os, json, re, pathlib, subprocess, tempfile
sys.path.insert(0, os.path.dirname(__file__))

from refresh_fo_cloud import _premium_estimate

REPO_ROOT = pathlib.Path(__file__).parent.parent
HTML_FILE = REPO_ROOT / "nse_live_dashboard.html"
TOLERANCE = 1  # rupee; banker's-vs-half-up rounding only, see docstring


def _build_cases():
    """Grid spanning deep-ITM to deep-OTM, both directions, short and long
    expiry, low and high vol — plus the two real positions from the
    06-Jul-2026 incident as pinned regression cases."""
    cases = []
    spot = 24000.0
    for opt in ("CE", "PE"):
        for strike in (21600, 23760, 24000, 24240, 26400):
            for days in (1, 7, 23, 45):
                for vol in (0.11, 0.16, 0.30):
                    cases.append({"spot": spot, "strike": strike,
                                  "vol": vol, "days": days, "opt": opt})
    # The incident itself: NIFTY50 24300 CE — buggy JS said 285 (-9% vs 311
    # entry), fixed formula says 397 (+28%). If parity ever breaks here
    # again, this is the first place it shows.
    cases.append({"spot": 24412.25, "strike": 24300, "vol": 0.1181, "days": 24, "opt": "CE"})
    cases.append({"spot": 58240.75, "strike": 58400, "vol": 0.1605, "days": 23, "opt": "CE"})
    return cases


def _extract_js_function():
    html = HTML_FILE.read_text(encoding="utf-8")
    m = re.search(r"function _estimatePremium\([^)]*\)\{[\s\S]*?\n  \}", html)
    if not m:
        print("[FAIL] Could not find _estimatePremium() in nse_live_dashboard.html.")
        print("       Either it was renamed/removed (update this extractor), or the")
        print("       formatting changed enough to break the regex. Either way this")
        print("       check can no longer see the JS twin -- fix before merging.")
        sys.exit(1)
    src = m.group(0)
    if "return" not in src or len(src) > 3000:
        print("[FAIL] Extracted _estimatePremium() looks wrong (no return, or too long).")
        print("       Extractor regex needs updating. Extracted:")
        print(src[:500])
        sys.exit(1)
    return src


def _run_js(fn_src, cases):
    script = (
        fn_src
        + "\nconst cases = " + json.dumps(cases) + ";"
        + "\nconsole.log(JSON.stringify(cases.map(c => _estimatePremium(c.spot, c.strike, c.vol, c.days, c.opt))));"
    )
    tmp = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    try:
        tmp.write(script)
        tmp.close()
        out = subprocess.run(["node", tmp.name], capture_output=True, text=True)
        if out.returncode != 0:
            print("[FAIL] Node execution of the extracted JS failed:")
            print(out.stderr)
            sys.exit(1)
        return json.loads(out.stdout)
    finally:
        os.unlink(tmp.name)


def _intrinsic(c):
    if c["opt"] == "PE":
        return max(0.0, c["strike"] - c["spot"])
    return max(0.0, c["spot"] - c["strike"])


def main():
    cases = _build_cases()
    js_src = _extract_js_function()
    js_vals = _run_js(js_src, cases)
    py_vals = [_premium_estimate(c["spot"], c["strike"], c["vol"], c["days"], c["opt"]) for c in cases]

    failures = []
    for c, py, js in zip(cases, py_vals, js_vals):
        label = f"{c['opt']} spot={c['spot']} strike={c['strike']} vol={c['vol']} days={c['days']}"
        if abs(py - js) > TOLERANCE:
            failures.append(f"PARITY  {label}: python={py} js={js} (diff {abs(py - js):.0f})")
        intr = _intrinsic(c)
        # +1 headroom for rounding; the 04-Jul bug violated this by 100s of rupees
        if py < intr - 1:
            failures.append(f"FLOOR-PY {label}: premium {py} < intrinsic {intr:.0f}")
        if js < intr - 1:
            failures.append(f"FLOOR-JS {label}: premium {js} < intrinsic {intr:.0f}")

    if failures:
        print(f"[FAIL] {len(failures)} check(s) failed across {len(cases)} cases:")
        for f in failures[:20]:
            print("   " + f)
        if len(failures) > 20:
            print(f"   ... and {len(failures) - 20} more")
        print()
        print("The premium model lives in TWO places that must stay identical:")
        print("  - nse-trading-bot/refresh_fo_cloud.py :: _premium_estimate()")
        print("  - nse_live_dashboard.html             :: _estimatePremium()")
        print("If you changed one, mirror the change in the other before merging.")
        sys.exit(1)

    print(f"[OK] {len(cases)} cases: Python and JS premium estimates agree (max allowed diff {TOLERANCE})")
    print(f"[OK] intrinsic-value floor holds in both implementations")


if __name__ == "__main__":
    main()
