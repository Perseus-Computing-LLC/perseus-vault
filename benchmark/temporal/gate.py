#!/usr/bin/env python3
"""CI gate for Mimir's bi-temporal contract.

Runs the offline bi-temporal benchmark (run.py) and asserts the time-travel /
supersede invariant holds *exactly*. Unlike a fuzzy quality metric this is a
correctness contract — every check must pass:

  * as_of(mid)   returns the version live between the two writes (v1),
  * as_of(now)   returns the current version (v2),
  * as_of(before) reports the fact did not exist yet,
  * current recall is live-only — a superseded v1 never resurfaces,
  * current recall still finds the live v2.

A single miss means time-travel or supersede regressed (e.g. a superseded
version bleeding back into recall), so the threshold is 100%. Fast, offline,
no network or API key.

Exit 0 on pass, 1 on failure. Usage: python benchmark/temporal/gate.py [--bin PATH]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

MIN_ACCURACY = 1.0  # bi-temporal is a correctness contract: every check must pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=None)
    args = ap.parse_args()

    out = os.path.join(tempfile.gettempdir(), "mimir-temporal-gate.json")
    cmd = [sys.executable, str(HERE / "run.py"), "--out", out]
    if args.bin:
        cmd += ["--bin", args.bin]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        # run.py already exits non-zero on any failed check; surface its output.
        print(r.stderr[-1000:])
        print("FAIL: bi-temporal benchmark reported a failing check.")
        return 1

    try:
        data = json.loads(Path(out).read_text(encoding="utf-8"))
    except Exception as e:
        print(f"FAIL: could not read benchmark report: {e}")
        return 1

    accuracy = data.get("accuracy", 0.0)
    passed, total = data.get("checks_passed", 0), data.get("checks_total", 0)
    print(f"bi-temporal accuracy = {accuracy:.3f}  ({passed}/{total} checks)")

    if accuracy < MIN_ACCURACY:
        print(f"FAIL: accuracy {accuracy:.3f} < {MIN_ACCURACY} - time-travel/supersede "
              f"regressed (a superseded version may be resurfacing in recall).")
        return 1
    print("PASS: bi-temporal time-travel and supersede hold exactly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
