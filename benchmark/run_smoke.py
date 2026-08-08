#!/usr/bin/env python3
"""Run the bounded local benchmark portfolio and emit a claim-aware summary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.package.common.artifacts import validate_report



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
    if not isinstance(result, dict) or result.get("returncode") != 0:
        return False
    report_path = result.get("report")
    if not isinstance(report_path, str):
        return False
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            return False
        validate_report(report)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError, OverflowError):
        return False
    if report.get("passed") is not True or report.get("status") != "passed":
        return False
    nested = report.get("runs")
    if nested is not None:
        if not isinstance(nested, list) or not nested:
            return False
        for child in nested:
            if not isinstance(child, dict) or not isinstance(child.get("report"), str):
                return False
            if not run_passed({"returncode": child.get("returncode"), "report": child["report"]}):
                return False
    return True


def _public_summary(summary: dict) -> dict:
    if not isinstance(summary, dict):
        return {}
    allowed = {"passed", "checks_passed", "checks_total", "accuracy", "verdict", "status"}
    result = {key: summary[key] for key in allowed if key in summary}
    nested = summary.get("runs")
    if nested is not None:
        if not isinstance(nested, list):
            raise ValueError("nested smoke summary must be a list")
        result["runs"] = [
            {
                "returncode": item.get("returncode"),
                "summary": _public_summary(item.get("summary", {})),
            }
            for item in nested
            if isinstance(item, dict)
        ]
        if len(result["runs"]) != len(nested):
            raise ValueError("nested smoke summary contains a non-object")
    return result


def _public_run(result: dict) -> dict:
    public = {
        key: result[key]
        for key in ("returncode", "timeout", "stdout_sha256", "stderr_sha256")
        if key in result
    }
    public["summary"] = _public_summary(result.get("summary", {}))
    return public


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
        "runs": [_public_run(run_result) for run_result in runs],
        "passed": aggregate_passed(runs),
    }
    Path(args.out).write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"passed": summary["passed"], "runs": len(runs), "out": args.out}, sort_keys=True))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
