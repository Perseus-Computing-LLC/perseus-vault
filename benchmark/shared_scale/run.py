#!/usr/bin/env python3
"""Offline multi-agent shared-memory scale benchmark (issue #790)."""
import argparse
import concurrent.futures
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_AGENTS = 16
DEFAULT_LATENCY_BUDGET_MS = 250.0


def find_binary(explicit=None):
    candidates = [explicit, os.environ.get("PERSEUS_VAULT_BIN")]
    exe = "perseus-vault.exe" if os.name == "nt" else "perseus-vault"
    candidates += [str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("perseus-vault binary not found; build it or pass --bin")


class Client:
    def __init__(self, binary, db, name):
        self.p = subprocess.Popen([binary, "--db", db], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
        self.id = 0
        self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": name, "version": "1"}})
        self.send_notification("notifications/initialized")

    def send_notification(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n"); self.p.stdin.flush()

    def send(self, method, params):
        self.id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.id, "method": method, "params": params}) + "\n"); self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line: raise RuntimeError("perseus-vault closed MCP stream")
            response = json.loads(line)
            if response.get("id") == self.id:
                if "error" in response: raise RuntimeError(str(response["error"]))
                return response["result"]

    def call(self, name, arguments):
        result = self.send("tools/call", {"name": name, "arguments": arguments})
        return json.loads(result["content"][0]["text"])

    def close(self):
        self.p.stdin.close(); self.p.stdout.close(); self.p.wait(timeout=20)


def percentile(values, pct):
    if not values: return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * pct) - 1)]


def evaluate(report, latency_budget_ms=DEFAULT_LATENCY_BUDGET_MS):
    results = report["results"]
    found = sum(r["found_shared"] for r in results)
    hidden = sum(r["private_hidden"] for r in results)
    latencies = [r["latency_ms"] for r in results]
    quality = found / len(results) if results else 0.0
    privacy = hidden / len(results) if results else 0.0
    latency = {"mean_ms": round(statistics.mean(latencies), 3) if latencies else 0.0,
               "p95_ms": round(percentile(latencies, 0.95), 3), "max_ms": round(max(latencies), 3) if latencies else 0.0,
               "budget_ms": latency_budget_ms}
    return {"retrieval_quality": quality, "privacy_quality": privacy, "latency": latency,
            "passed": bool(results) and quality == 1.0 and privacy == 1.0 and latency["p95_ms"] <= latency_budget_ms}


def run(binary=None, agents=DEFAULT_AGENTS, latency_budget_ms=DEFAULT_LATENCY_BUDGET_MS):
    if agents < 2: raise ValueError("agents must be >= 2")
    binary = find_binary(binary)
    db = str(Path(tempfile.gettempdir()) / f"perseus-vault-shared-scale-{os.getpid()}.db")
    for ext in ("", "-wal", "-shm"):
        Path(db + ext).unlink(missing_ok=True)
    writer = Client(binary, db, "scale-writer")
    try:
        writer.call("perseus_vault_remember", {"category":"scale_shared", "key":"shared-fact", "body_json":json.dumps({"note":"Shared scale anchor is available to every workspace agent."}), "workspace_hash":"scale", "skip_dedup":True})
        writer.call("perseus_vault_remember", {"category":"scale_private", "key":"private-fact", "body_json":json.dumps({"note":"Private scale anchor must stay hidden."}), "workspace_hash":"scale", "agent_id":"scale-writer", "visibility":"private", "skip_dedup":True})
        def exercise(i):
            client = Client(binary, db, f"scale-agent-{i}")
            try:
                started = time.perf_counter()
                shared = client.call("perseus_vault_recall", {"query":"shared scale anchor", "workspace_hash":"scale", "limit":10})
                elapsed = (time.perf_counter() - started) * 1000
                private = client.call("perseus_vault_recall", {"query":"private scale anchor", "workspace_hash":"scale", "limit":10})
                return {"agent":f"scale-agent-{i}", "found_shared":any(x.get("key") == "shared-fact" for x in shared.get("items", [])), "private_hidden":not any(x.get("key") == "private-fact" for x in private.get("items", [])), "latency_ms":round(elapsed, 3)}
            finally: client.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=agents) as pool:
            results = list(pool.map(exercise, range(agents)))
    finally: writer.close()
    report = {"benchmark":"perseus-vault-shared-memory-scale", "agents":agents, "assumptions":{"agents":"concurrent isolated MCP clients sharing one SQLite vault", "workspace":"one workspace with one private writer record", "limit":"not a distributed-load benchmark"}, "results":results}
    return {**report, **evaluate(report, latency_budget_ms)}


def main():
    p=argparse.ArgumentParser(); p.add_argument("--bin"); p.add_argument("--agents", type=int, default=DEFAULT_AGENTS); p.add_argument("--latency-budget-ms", type=float, default=DEFAULT_LATENCY_BUDGET_MS); p.add_argument("--out")
    a=p.parse_args(); report=run(a.bin, a.agents, a.latency_budget_ms)
    if a.out: Path(a.out).write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2)); return 0 if report["passed"] else 1

if __name__ == "__main__": sys.exit(main())
