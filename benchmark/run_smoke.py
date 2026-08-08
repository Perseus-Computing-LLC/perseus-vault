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
    try:
        completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {
            "command": command,
            "returncode": None,
            "timeout": True,
            "stdout_sha256": "",
            "stderr_sha256": "",
            "report": str(output),
            "summary": {"passed": False, "failure_class": "subprocess_timeout"},
        }
    result = {
        "command": command,
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
        "report": str(output),
    }
    if output.exists():
        try:
            report = json.loads(output.read_text())
            result["summary"] = {
                key: report[key]
                for key in ("passed", "checks_passed", "checks_total", "accuracy", "verdict", "status")
                if key in report
            }
            if "runs" in report:
                result["summary"]["runs"] = [
                    {
                        "returncode": run_result.get("returncode"),
                        "summary": {
                            key: run_result.get("summary", {}).get(key)
                            for key in ("passed", "checks_passed", "checks_total", "accuracy", "verdict", "status")
                            if key in run_result.get("summary", {})
                        },
                    }
                    for run_result in report["runs"]
                ]
        except json.JSONDecodeError:
            result["summary"] = {"parse_error": True}
    return result


def run_passed(result: dict) -> bool:
    summary = result.get("summary", {})
    if result.get("returncode") != 0 and not (summary.get("passed") is True and summary.get("status") == "passed"):
        return False
    if summary.get("passed") is False or summary.get("status") in {"failed", "blocked", "partial"} or summary.get("verdict") == "blocked":
        return False
    nested = summary.get("runs", [])
    return all(
        (item.get("returncode") == 0 or (item.get("summary", {}).get("passed") is True and item.get("summary", {}).get("status") == "passed"))
        and item.get("summary", {}).get("passed") is not False
        for item in nested
    )


def aggregate_passed(results: list[dict]) -> bool:
    return all(run_passed(item) for item in results)


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
    summary = {"benchmark": "perseus-vault-local-smoke", "runs": runs, "passed": aggregate_passed(runs)}
    Path(args.out).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"passed": summary["passed"], "runs": len(runs), "out": args.out}, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
