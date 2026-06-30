"""
Sector rotation refresh — writes sector_rotation.json to the repo root so
the dashboard can fetch it, the same pattern as refresh_fo_cloud.py and
trade_brain.py. Runs on the same CI cron.
"""
import sys, os, json, pathlib
sys.path.insert(0, os.path.dirname(__file__))

from sector_rotation_core import scan_sector_rotation

REPO_ROOT = pathlib.Path(__file__).parent.parent
OUT_FILE = REPO_ROOT / "sector_rotation.json"


def main():
    result = scan_sector_rotation(top_n=3, stocks_per_sector=3)
    OUT_FILE.write_text(json.dumps(result, indent=2))
    top = ", ".join(result["top_sectors"])
    print(f"  Top sectors: {top}")
    if result["sectors_with_errors"]:
        print(f"  Errors: {result['sectors_with_errors']}")
    print(f"  Wrote {OUT_FILE}")


if __name__ == "__main__":
    main()
