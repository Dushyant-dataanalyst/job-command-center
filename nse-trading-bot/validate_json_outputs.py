"""
Sanity-checks every JSON file this pipeline writes, right before the commit
step stages them. Deliberately loose validation (parseable + right top-level
type + a couple of required keys), not a strict schema contract — the
scripts' own output shapes have changed multiple times this session, and an
over-specified schema would cause false rejections every time a script
legitimately adds a field. This only guards against the failure modes that
actually matter: a truncated/corrupted write, or a script crash leaving an
empty file.

If a file fails validation, it's restored from the last commit on origin
(git show origin/master:<file>) before staging — so a corrupted write never
gets propagated forward and overwrites the last known-good version.
"""
import sys, os, json, pathlib, subprocess
sys.path.insert(0, os.path.dirname(__file__))

REPO_ROOT = pathlib.Path(__file__).parent.parent

# filename -> (expected top-level type, required keys if dict)
SCHEMA = {
    "fo_latest.json":            (dict, []),
    "trade_journal.json":        (dict, ["trades", "stats"]),
    "sector_rotation.json":      (dict, []),
    "market_mood.json":          (dict, ["fetched_at"]),
    "news_feed.json":            (dict, []),
    "equity_journal.json":       (dict, ["positions", "stats"]),
    "stock_fo.json":             (dict, []),
    "equity_scan.json":          (dict, ["_meta"]),
    "equity_scan_history.json":  (list, []),
    "market_regime.json":        (dict, ["instruments", "recommendation"]),
    "voter_weights.json":        (dict, ["voters"]),
    "kite_portfolio.json":       (dict, ["session_live", "fetched_at"]),
    "kite_trade_history.json":   (list, []),
    "recommendation_journal.json": (dict, ["recommendations", "summary"]),
    "expert_gate.json":          (dict, ["instruments"]),
    "strategy_performance.json": (dict, ["overall"]),
    "macro_risk.json":           (dict, ["fetched_at", "risk_score", "risk_level"]),
    "astro_view.json":           (dict, ["fetched_at"]),
    "context_score.json":        (dict, ["fo_index", "fo_stock", "equity"]),
}


def _validate_one(path):
    if not path.exists():
        return False, "file does not exist"
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        return False, f"could not read file: {e}"
    if not raw.strip():
        return False, "file is empty"
    try:
        data = json.loads(raw)
    except Exception as e:
        return False, f"invalid JSON: {e}"

    expected_type, required_keys = SCHEMA.get(path.name, (None, []))
    if expected_type is not None and not isinstance(data, expected_type):
        return False, f"expected top-level {expected_type.__name__}, got {type(data).__name__}"
    if expected_type is dict:
        missing = [k for k in required_keys if k not in data]
        if missing:
            return False, f"missing required keys: {missing}"
    return True, "OK"


def _restore_from_git(filename):
    try:
        result = subprocess.run(
            ["git", "show", f"origin/master:{filename}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        (REPO_ROOT / filename).write_text(result.stdout, encoding="utf-8")
        return True
    except Exception:
        return False


def main():
    any_invalid = False
    for filename in SCHEMA:
        path = REPO_ROOT / filename
        ok, detail = _validate_one(path)
        if ok:
            print(f"  [OK] {filename}: {detail}")
        else:
            any_invalid = True
            print(f"  [INVALID] {filename}: {detail}")
            restored = _restore_from_git(filename)
            if restored:
                print(f"    -> restored {filename} from last commit on origin/master")
            else:
                print(f"    -> could not restore {filename} from git (may not exist there yet either)")
    sys.exit(1 if any_invalid else 0)


if __name__ == "__main__":
    main()
