#!/usr/bin/env python3
"""Run the bounded local benchmark portfolio and emit a claim-aware summary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import signal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(command: list[str], output: Path) -> dict:
    output.unlink(missing_ok=True)
    process_group = os.name != "nt"
    process = None
    try:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=process_group,
        )
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        if process is not None:
            if process_group:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                if process_group:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()
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
        "returncode": process.returncode,
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
        "report": str(output),
    }
    if output.exists():
        try:
            report = json.loads(output.read_text())
            if not isinstance(report, dict):
                raise ValueError("report must be an object")
            result["summary"] = {
                key: report[key]
                for key in ("passed", "checks_passed", "checks_total", "accuracy", "verdict", "status")
                if key in report
            }
            if "passed" not in report or report.get("status") != "passed":
                result["summary"]["passed"] = False
                result["summary"]["status"] = report.get("status", "missing")
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
        except (json.JSONDecodeError, OSError, ValueError):
            result["summary"] = {"parse_error": True}
    else:
        result["summary"] = {"parse_error": True}
    return result


def run_passed(result: dict) -> bool:
    summary = result.get("summary", {})
    if result.get("returncode") != 0:
        return False
    if summary.get("passed") is not True or summary.get("status") != "passed" or summary.get("verdict") == "blocked":
        return False
    if "runs" in summary:
        nested = summary.get("runs")
        if not isinstance(nested, list) or not nested:
            return False
        return all(
            isinstance(item, dict)
            and isinstance(item.get("summary"), dict)
            and item.get("returncode") == 0
            and item["summary"].get("passed") is True
            and item["summary"].get("status") == "passed"
            for item in nested
        )
    report_path = result.get("report")
    if not isinstance(report_path, str):
        return False
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    required = {"schema_version", "benchmark_id", "suite_version", "cases", "metrics", "result_signature_sha256", "claims_sha256"}
    if not isinstance(report, dict) or not required.issubset(report):
        return False
    return True


def aggregate_passed(results: list[dict]) -> bool:
    return bool(results) and all(run_passed(item) for item in results)


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
    summary = {
        "benchmark": "perseus-vault-local-smoke",
        "status": "passed" if aggregate_passed(runs) else "failed",
        "runs": runs,
        "passed": aggregate_passed(runs),
    }
    Path(args.out).write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"passed": summary["passed"], "runs": len(runs), "out": args.out}, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
