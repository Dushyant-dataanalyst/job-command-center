"""
context_score_dryrun.py — read-only, on-demand verification for
context_score.py, same convention as backtest.py/check_premium_parity.py.
NOT wired into CI.

context_score.py has no executor to backtest P&L against (no order code
exists -- SEBI static-IP block). The honest question this script answers
instead: does context_score's computed state/score actually correlate with
what LATER happened to the recommendation it was read against?

Reconstructs N recent CI-refresh commits' point-in-time state for the 8
files context_score.py reads, runs compute_context_score() (context_score
.py's PURE function, not its main()'s I/O -- same split as expert_gate.py's
advance()/main()) against each, and prints a human-reviewable table:
raw_signal vs computed state/score at that moment vs the ACTUAL later
outcome (won/lost/expired/invalidated) read from TODAY's recommendation_
journal.json, matched by kind+symbol+closest opened_at (recommendation
ids are timestamp-suffixed at creation, so they don't match stably across
runs -- this is a documented best-effort match, not an exact one).

WHY GIT HISTORY, NOT snapshots/: every one of these 8 files is already
committed on every refresh (see the workflow's own `git add` list), so
real history already exists back to whenever each file was first added --
deeper and more complete than snapshots/, which only recently gained the
4 learning-engine feeds (see snapshot_daily.py's own 09-Jul-2026 change)
and only keeps 30 days.

MANUAL REVIEW ONLY. This script does not gate, pass/fail, or wire
context_score.py into CI itself -- read the printed table yourself before
deciding whether to register context_score.json in validate_json_outputs
.py / vercel.json / the workflow. Same "audit -> approval -> validate
first -> wire in" order as backtest.py/trade_filters.py before it.

Usage: python context_score_dryrun.py [--commits N]
"""
import sys, os, json, pathlib, subprocess, argparse
sys.path.insert(0, os.path.dirname(__file__))

from datetime import datetime

from context_score import compute_context_score

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = pathlib.Path(__file__).parent / "context_score_dryrun_output.json"

FILES = ["fo_latest.json", "stock_fo.json", "equity_scan.json", "market_regime.json",
         "expert_gate.json", "macro_risk.json", "strategy_performance.json",
         "recommendation_journal.json"]


def _run(cmd):
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8")
    return result.stdout, result.returncode


def _recent_commits(limit):
    """Commits touching fo_latest.json (every real refresh writes it), most
    recent first."""
    out, code = _run(["git", "log", "--format=%H", "--", "fo_latest.json"])
    if code != 0 or not out.strip():
        return []
    return out.strip().splitlines()[:limit]


def _show_json(commit, filename):
    out, code = _run(["git", "show", f"{commit}:{filename}"])
    if code != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def _commit_datetime(commit):
    out, code = _run(["git", "show", "-s", "--format=%cI", commit])
    if code == 0 and out.strip():
        try:
            return datetime.fromisoformat(out.strip()).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def _valid_macro(raw):
    """Mirrors macro_gate.load_macro_risk()'s own validation -- this script
    bypasses that function (it reads a historical git blob, not the live
    file) so the same "None on missing/invalid" contract must be applied
    here by hand, not skipped."""
    if not isinstance(raw, dict) or raw.get("risk_score") is None:
        return None
    return raw


def _opened_dt(rec):
    try:
        return datetime.strptime((rec.get("opened_at") or "").replace(" IST", ""), "%d %b %Y %H:%M")
    except Exception:
        return None


def _closest_later_outcome(today_recs, kind, symbol, at_dt):
    candidates = [r for r in today_recs if r.get("kind") == kind and r.get("symbol") == symbol
                  and r.get("status") in ("won", "lost", "expired", "invalidated")]
    dated = [(r, _opened_dt(r)) for r in candidates]
    dated = [(r, d) for r, d in dated if d is not None]
    if not dated:
        return None
    dated.sort(key=lambda rd: abs((rd[1] - at_dt).total_seconds()))
    return dated[0][0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commits", type=int, default=20, help="how many recent refresh commits to replay")
    args = parser.parse_args()

    commits = _recent_commits(args.commits)
    if not commits:
        print("No commits touching fo_latest.json found in this repo -- nothing to replay.")
        return
    print(f"Replaying {len(commits)} recent refresh commit(s)...")

    journal_path = REPO_ROOT / "recommendation_journal.json"
    today_journal = json.loads(journal_path.read_text(encoding="utf-8")) if journal_path.exists() else {}
    today_recs = today_journal.get("recommendations", []) if isinstance(today_journal, dict) else []
    if not isinstance(today_recs, list):
        today_recs = []

    rows = []
    for commit in commits:
        at_dt = _commit_datetime(commit)
        fo = _show_json(commit, "fo_latest.json")
        stock_fo = _show_json(commit, "stock_fo.json")
        equity = _show_json(commit, "equity_scan.json")
        regime = _show_json(commit, "market_regime.json")
        expert_gate_data = _show_json(commit, "expert_gate.json")
        macro = _valid_macro(_show_json(commit, "macro_risk.json"))
        strategy_perf = _show_json(commit, "strategy_performance.json")
        journal = _show_json(commit, "recommendation_journal.json") or {}
        journal_recs = journal.get("recommendations", []) if isinstance(journal, dict) else []
        if not isinstance(journal_recs, list):
            journal_recs = []

        try:
            per_kind = compute_context_score(fo, stock_fo, equity, regime, expert_gate_data,
                                              macro, journal_recs, strategy_perf, at_dt)
        except Exception as e:
            print(f"  SKIP {commit[:8]} ({at_dt}): compute_context_score raised {e}")
            continue

        for kind in ("fo_index", "fo_stock", "equity"):
            for sym, entry in per_kind[kind].items():
                # Only look up a later outcome for rows where a recommendation
                # would actually have been opened at this moment -- an
                # actionable raw signal, OR a state that itself implies an
                # existing tracked rec (HOLD/EXIT_WATCH/COOLDOWN, derived from
                # a real open/closed journal entry -- see context_score.py's
                # _stateless_lifecycle_state). A bare WATCH/NO_TRADE read has
                # no corresponding rec to validate against; matching it to
                # "whatever closed rec happened to be nearest in time" (the
                # first version of this script did exactly that) produces a
                # spurious, misleading later_outcome -- caught during manual
                # review of the first real run, fixed before trusting this
                # tool going forward.
                raw_actionable = entry["raw_signal"] in ("BUY_CE", "BUY_PE", "BUY", "STRONG_BUY")
                trackable = raw_actionable or entry["state"] in ("HOLD", "EXIT_WATCH", "EXIT_CONFIRMED", "COOLDOWN")
                later = _closest_later_outcome(today_recs, kind, sym, at_dt) if trackable else None
                rows.append({
                    "commit": commit[:8], "at": str(at_dt), "kind": kind, "symbol": sym,
                    "raw_signal": entry["raw_signal"], "state": entry["state"],
                    "score": entry["context_score"],
                    "later_outcome": later.get("status") if later else None,
                    "later_pct": later.get("outcome_pct") if later else None,
                })

    _print_table(rows)
    OUT_FILE.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"\n  {len(rows)} row(s) written to {OUT_FILE}")
    print("  MANUAL REVIEW REQUIRED before context_score.py joins the live CI pipeline -- "
          "this script does not pass/fail anything automatically.")


def _print_table(rows):
    if not rows:
        print("No rows produced -- either no commits had usable data, or nothing was actionable in the replayed window.")
        return
    print(f"\n{'commit':9s} {'kind':9s} {'symbol':12s} {'raw':10s} {'state':16s} {'score':>6s}  {'later_outcome':14s} {'later_pct':>9s}")
    for r in rows:
        print(f"{r['commit']:9s} {r['kind']:9s} {r['symbol']:12s} {r['raw_signal']:10s} {r['state']:16s} "
              f"{str(r['score']):>6s}  {str(r['later_outcome'] or '-'):14s} {str(r['later_pct'] or '-'):>9s}")


if __name__ == "__main__":
    main()
