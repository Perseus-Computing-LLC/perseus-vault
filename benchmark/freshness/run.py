#!/usr/bin/env python3
"""Freshness and failure-behavior benchmark.

Measures write-to-FTS readability and records explicit RecallOutcome states for
FTS and hybrid paths. It also probes bounded deadlines and process restart.
Embedding-provider failure injection is an opt-in lane because the benchmark
must not pretend an unavailable provider was exercised when the local binary
has no injectable provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((percentile / 100.0) * len(ordered) + 0.999999))
    return ordered[min(rank, len(ordered)) - 1]


def classify_outcome(outcome: Any) -> str:
    if not isinstance(outcome, dict) or "status" not in outcome:
        return "missing"
    status = str(outcome.get("status", "")).lower()
    if status == "stale" and outcome.get("abstained") is True:
        return "stale_abstained"
    if status == "fresh":
        return "fresh"
    if status == "empty":
        return "empty"
    if status == "partial":
        return "partial"
    if status == "timeout":
        return "timeout"
    if status == "unavailable":
        return "unavailable"
    return status or "missing"


def freshness_signature(rows: list[dict[str, Any]]) -> str:
    payload = [
        {"case": row.get("case"), "axis": row.get("axis"), "ok": bool(row.get("ok"))}
        for row in sorted(rows, key=lambda row: (str(row.get("case", "")), str(row.get("axis", ""))))
    ]
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def find_binary(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("PERSEUS_VAULT_BIN"), os.environ.get("MIMIR_BIN")]
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        candidates.extend((str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("perseus-vault binary not found; build it or pass --bin")


class Client:
    def __init__(self, binary: str, db: Path):
        self.p = subprocess.Popen([binary, "--db", str(db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.i = 0
        self.send({"jsonrpc": "2.0", "id": self.next_id(), "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "freshness-benchmark", "version": "1"}}})
        self.read()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def next_id(self) -> int:
        self.i += 1
        return self.i

    def send(self, message: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.p.stdin.flush()

    def read(self) -> dict[str, Any]:
        assert self.p.stdout is not None
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("Vault closed MCP stream")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError("MCP request failed")
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError("MCP tool returned an error")
        if isinstance(result, dict) and result.get("content"):
            text = result["content"][0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
        return result

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": self.next_id(), "method": "tools/call", "params": {"name": name, "arguments": args}})
        return self.read()

    def close(self) -> None:
        try:
            if self.p.stdin is not None:
                self.p.stdin.close()
            self.p.wait(timeout=30)
        except Exception:
            self.p.kill()
            self.p.wait(timeout=5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=None)
    ap.add_argument("--out", default=str(HERE / "report.json"))
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--deadline-ms", type=int, default=1)
    args = ap.parse_args()
    binary = find_binary(args.bin)
    db = Path(tempfile.mkdtemp(prefix="perseus-vault-freshness-")) / "freshness.db"
    rows: list[dict[str, Any]] = []
    fts_lags: list[float] = []
    outcome_classes: dict[str, int] = {}
    c = Client(binary, db)
    category = "benchmark_freshness"
    try:
        for index in range(max(1, args.samples)):
            key = f"freshness-{index}"
            marker = f"quality-freshness-marker-{index}-9e4a"
            started = time.perf_counter()
            c.call("perseus_vault_remember", {"category": category, "key": key, "body_json": stable_json({"marker": marker}), "skip_dedup": True})
            write_ms = (time.perf_counter() - started) * 1000
            visible = False
            result: dict[str, Any] = {}
            for _ in range(20):
                result = c.call("perseus_vault_recall", {"query": marker.replace("-", " "), "category": category, "mode": "fts5", "limit": 20, "min_decay": 0, "trust_weight": 0, "include_outcome": True})
                visible = marker in stable_json(result.get("items", []))
                if visible:
                    break
            lag = max(0.0, (time.perf_counter() - started) * 1000)
            fts_lags.append(lag)
            rows.append({"case": key, "axis": "write_to_fts_readable", "ok": visible, "elapsed_ms": lag, "write_ms": write_ms})
            outcome = result.get("outcome") if isinstance(result, dict) else None
            classification = classify_outcome(outcome)
            outcome_classes[classification] = outcome_classes.get(classification, 0) + 1
            rows.append({"case": key, "axis": "fts_outcome_explicit", "ok": classification != "missing", "outcome_class": classification})
        deadline = c.call("perseus_vault_recall", {"query": "benchmark freshness absent deadline", "category": category, "mode": "hybrid", "limit": 100, "deadline_ms": args.deadline_ms, "include_outcome": True})
        deadline_class = classify_outcome(deadline.get("outcome") if isinstance(deadline, dict) else None)
        rows.append({"case": "deadline", "axis": "deadline_outcome_explicit", "ok": deadline_class in {"timeout", "fresh", "partial", "stale", "empty", "unavailable"}, "outcome_class": deadline_class})
    finally:
        c.close()
    restarted = Client(binary, db)
    try:
        probe = restarted.call("perseus_vault_recall", {"query": "quality freshness marker", "category": category, "mode": "fts5", "limit": 100, "include_outcome": True})
        rows.append({"case": "restart", "axis": "readable_after_restart", "ok": "quality-freshness-marker" in stable_json(probe.get("items", []))})
        rows.append({"case": "restart", "axis": "restart_outcome_explicit", "ok": classify_outcome(probe.get("outcome")) != "missing"})
    finally:
        restarted.close()
    passed = sum(bool(row["ok"]) for row in rows)
    report = {
        "benchmark": "perseus-vault-freshness",
        "suite_version": "v1",
        "binary": Path(binary).name,
        "offline": True,
        "network_calls": 0,
        "checks_passed": passed,
        "checks_total": len(rows),
        "accuracy": round(passed / len(rows), 4) if rows else 0.0,
        "metrics": {"write_to_fts_ms": {"p50_ms": percentile_nearest_rank(fts_lags, 50), "p95_ms": percentile_nearest_rank(fts_lags, 95), "p99_ms": percentile_nearest_rank(fts_lags, 99), "samples": len(fts_lags)}, "outcome_classes": outcome_classes},
        "cases": rows,
        "signature_sha256": freshness_signature(rows),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"checks_passed": passed, "checks_total": len(rows), "accuracy": report["accuracy"], "p50_ms": report["metrics"]["write_to_fts_ms"]["p50_ms"], "out": args.out}, sort_keys=True))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
