#!/usr/bin/env python3
"""Bounded offline resource collector for the #1105 benchmark."""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import signal
import subprocess
import threading
import time
from typing import Any

try:
    import resource as _resource
except ImportError:  # Windows: report child CPU as unavailable.
    _resource = None

from benchmark.resource_envelope import harness

DRIVER_WORKLOAD_FIELDS = {
    "measurement_overhead_ns",
    "recall_candidate_count",
    "selected_count",
    "traversal_depth",
    "context_assembly_ns",
    "output_bytes",
    "output_tokens",
}


def _available(value: float) -> dict[str, Any]:
    return {"status": "available", "value": value}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


class _LinuxProcessSampler:
    """Best-effort bounded /proc sampler for one child process."""

    def __init__(self, pid: int):
        self.pid = pid
        self.peak_rss_bytes: int | None = None
        self.read_bytes: int | None = None
        self.write_bytes: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _sample(self) -> None:
        root = pathlib.Path("/proc") / str(self.pid)
        try:
            for line in (root / "status").read_text(encoding="utf-8").splitlines():
                if line.startswith(("VmRSS:", "VmHWM:")):
                    value = int(line.split()[1]) * 1024
                    self.peak_rss_bytes = max(self.peak_rss_bytes or 0, value)
            for line in (root / "io").read_text(encoding="utf-8").splitlines():
                name, value = line.split(":", 1)
                if name == "read_bytes":
                    self.read_bytes = max(self.read_bytes or 0, int(value.strip()))
                elif name == "write_bytes":
                    self.write_bytes = max(self.write_bytes or 0, int(value.strip()))
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            return

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            self._stop.wait(0.005)
        self._sample()


def _child_cpu_ns(before: Any, after: Any) -> int:
    delta = (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)
    return max(0, int(delta * 1_000_000_000))


def _parse_driver_output(stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise harness.ContractError("driver returned no JSON result")
    try:
        raw = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise harness.ContractError("driver result is not JSON") from exc
    if not isinstance(raw, dict):
        raise harness.ContractError("driver result must be an object")
    status = raw.get("outcome_status")
    if status not in harness.OUTCOME_STATUSES:
        raise harness.ContractError("driver returned unsupported outcome_status")
    workload = raw.get("workload")
    if not isinstance(workload, dict) or not DRIVER_WORKLOAD_FIELDS.issubset(workload):
        raise harness.ContractError("driver workload fields are incomplete")
    clean_workload: dict[str, int | float] = {}
    for field in DRIVER_WORKLOAD_FIELDS:
        value = workload[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise harness.ContractError(f"driver workload {field} is invalid")
        clean_workload[field] = value
    network_calls = raw.get("network_calls")
    if network_calls != 0:
        raise harness.ContractError("offline driver reported network activity")
    return {"outcome_status": status, "workload": clean_workload, "network_calls": 0}


def collect_case(
    driver_command: list[str],
    case: dict[str, Any],
    *,
    repetition: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Execute one bounded driver cell and emit a sanitized observation."""

    if not driver_command or timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
        raise harness.ContractError("collector command and timeout must be bounded")
    case_json = json.dumps(case, sort_keys=True, separators=(",", ":"))
    command = [
        *driver_command,
        "--case-json",
        case_json,
        "--repetition",
        str(repetition),
    ]
    before_cpu = (
        _resource.getrusage(_resource.RUSAGE_CHILDREN)
        if _resource is not None
        else None
    )
    started = time.perf_counter_ns()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=(os.name != "nt"),
        env={**os.environ, "PERSEUS_RESOURCE_ENVELOPE_OFFLINE": "1"},
    )
    sampler = _LinuxProcessSampler(process.pid)
    sampler.start()
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = process.communicate(timeout=5)
    finally:
        sampler.stop()
    wall_time_ns = max(1, time.perf_counter_ns() - started)
    after_cpu = (
        _resource.getrusage(_resource.RUSAGE_CHILDREN)
        if _resource is not None
        else None
    )
    cpu_time_ns = (
        _child_cpu_ns(before_cpu, after_cpu)
        if before_cpu is not None and after_cpu is not None
        else None
    )
    resources = {
        "wall_time_ns": _available(wall_time_ns),
        "cpu_time_ns": (
            _available(cpu_time_ns)
            if cpu_time_ns is not None
            else _unavailable("child_cpu_sampler_unavailable")
        ),
        "peak_rss_bytes": (
            _available(sampler.peak_rss_bytes)
            if sampler.peak_rss_bytes is not None
            else _unavailable("process_rss_sampler_unavailable")
        ),
        "read_bytes": (
            _available(sampler.read_bytes)
            if sampler.read_bytes is not None
            else _unavailable("process_io_sampler_unavailable")
        ),
        "write_bytes": (
            _available(sampler.write_bytes)
            if sampler.write_bytes is not None
            else _unavailable("process_io_sampler_unavailable")
        ),
        "power_watts": _unavailable("power_sensor_not_configured"),
        "energy_joules": _unavailable("energy_sensor_not_configured"),
    }
    if timed_out:
        overhead = 0
        workload = {
            name: _unavailable("driver_timeout")
            for name in DRIVER_WORKLOAD_FIELDS
            if name != "measurement_overhead_ns"
        }
        workload["measurement_overhead_ns"] = _available(overhead)
        outcome_status = "timeout"
        network_calls = 0
    elif process.returncode != 0:
        raise harness.ContractError(
            f"driver exited nonzero ({process.returncode}); stderr_sha256="
            f"{__import__('hashlib').sha256(stderr.encode()).hexdigest()}"
        )
    else:
        result = _parse_driver_output(stdout)
        outcome_status = result["outcome_status"]
        network_calls = result["network_calls"]
        workload = {
            name: _available(value) for name, value in result["workload"].items()
        }
        overhead = int(result["workload"]["measurement_overhead_ns"])
    workload["net_wall_time_ns"] = _available(max(0, wall_time_ns - overhead))
    return {
        "schema_version": harness.OBSERVATION_SCHEMA,
        "case_id": case["id"],
        "repetition": repetition,
        "outcome_status": outcome_status,
        "network_calls": network_calls,
        "resources": resources,
        "workload": workload,
    }


def collect_manifest(
    manifest: dict[str, Any], driver_command: list[str], timeout_seconds: float
) -> list[dict[str, Any]]:
    normalized = harness.validate_manifest(manifest)
    observations = []
    for case in normalized["cases"]:
        for repetition in range(case["repetitions"]):
            observations.append(
                collect_case(
                    driver_command,
                    case,
                    repetition=repetition,
                    timeout_seconds=timeout_seconds,
                )
            )
    return observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--driver-command-json",
        required=True,
        help="JSON array argv prefix; collector appends --case-json/--repetition",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    manifest = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    driver_command = json.loads(args.driver_command_json)
    if (
        not isinstance(driver_command, list)
        or not driver_command
        or any(not isinstance(value, str) or not value for value in driver_command)
    ):
        raise harness.ContractError(
            "driver-command-json must be a non-empty string array"
        )
    observations = collect_manifest(manifest, driver_command, args.timeout_seconds)
    pathlib.Path(args.out).write_text(
        json.dumps(observations, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"observations": len(observations), "status": "complete"}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
