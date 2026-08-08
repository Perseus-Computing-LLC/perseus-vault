#!/usr/bin/env python3
"""Run the bounded local benchmark portfolio and emit a claim-aware summary."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(command: list[str], output: Path) -> dict:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    result = {
        "command": command,
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "report": str(output),
    }
    if output.exists():
        try:
            result["summary"] = json.loads(output.read_text())
        except json.JSONDecodeError:
            result["summary"] = {"parse_error": True}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", default="target/release/perseus-vault")
    parser.add_argument("--out", default="/tmp/perseus-vault-benchmark-smoke-summary.json")
    args = parser.parse_args()
    runs = []
    commands = [
        (["python3", "benchmark/quality/run.py", "--bin", args.bin, "--out", "/tmp/perseus-quality-smoke.json"], Path("/tmp/perseus-quality-smoke.json")),
        (["python3", "benchmark/correction/run.py", "--bin", args.bin, "--out", "/tmp/perseus-correction-smoke.json"], Path("/tmp/perseus-correction-smoke.json")),
        (["python3", "benchmark/deletion/run.py", "--bin", args.bin, "--out", "/tmp/perseus-deletion-smoke.json"], Path("/tmp/perseus-deletion-smoke.json")),
        (["python3", "benchmark/freshness/run.py", "--bin", args.bin, "--out", "/tmp/perseus-freshness-smoke.json", "--samples", "8"], Path("/tmp/perseus-freshness-smoke.json")),
    ]
    for command, output in commands:
        runs.append(run(command, output))
    summary = {"benchmark": "perseus-vault-local-smoke", "runs": runs, "passed": all(item["returncode"] == 0 for item in runs)}
    Path(args.out).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"passed": summary["passed"], "runs": len(runs), "out": args.out}, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
