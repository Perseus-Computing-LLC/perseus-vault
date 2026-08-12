#!/usr/bin/env python3
"""Perseus Vault #952: maintenance throughput / serving-isolation benchmark.

Pins the consolidation operating point on a FIXED corpus and measures the
live-recall probe before/after a full-budget run — the two numbers the
maintenance-serving-isolation spec (docs/specs/maintenance-serving-isolation.md)
commits to:

  * consolidation throughput (entities examined / second, full budget, no
    guard env set), and
  * recall p95 before vs after the run (the SLO the gate protects: a run must
    not degrade live recall beyond ~1.5x baseline).

Fully offline and deterministic: drives the real `perseus_vault` binary over
MCP stdio, ingests a seeded corpus (same content every run), runs
perseus_vault_consolidate at full budget, and reports JSON + a markdown block
ready to paste into the spec's operating-point section.

Usage:
    cargo build --release
    python benchmark/maintenance/throughput.py                 # auto-locate
    python benchmark/maintenance/throughput.py --bin /path/to/perseus-vault
    PERSEUS_VAULT_BIN=/path/to/perseus-vault python benchmark/maintenance/throughput.py
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

CORPUS_SIZE = 200          # entities, fixed
CATEGORIES = 20            # categories x 10 entities each
SEED_TOPICS = [
    # overlapping content so consolidation finds real near-duplicate groups
    ("facts", "the deployment pipeline uses canary releases for staging"),
    ("facts", "canary releases roll out to staging before production"),
    ("convention", "always run the test suite before merging a feature branch"),
    ("convention", "run tests before merging branches into main"),
    ("insight", "bounded single-reviewer workflows reduce stale review queues"),
    ("insight", "single reviewer keeps review queues from going stale"),
    ("decision", "adopt rolling deploys for the control plane services"),
    ("decision", "control plane will use rolling deployment strategy"),
    ("lesson", "terminal guards crash on slash-containing executables"),
    ("lesson", "guard processes fail on absolute paths with slashes"),
    ("episodes", "the dashboard busy-loop consumed a full core until restart"),
    ("episodes", "dashboard spin loop fixed by bouncing the container"),
    ("preference", "progressive disclosure dashboards put actions first"),
    ("preference", "landing dashboards show actions before detail"),
    ("observation", "maintenance runs serialize per store in the vault"),
    ("observation", "vault maintenance is serialized and never reserved"),
    ("memories", "env overrides are thread local in the test suite"),
    ("memories", "tests use thread local overrides for gate config"),
    ("project", "the recall contract spec pins hybrid fusion semantics"),
    ("project", "recall serving contract documents the fusion path"),
]


def find_binary(explicit):
    cands = [explicit, os.environ.get("PERSEUS_VAULT_BIN")]
    for name in ("perseus-vault", "perseus_vault"):
        exe = f"{name}.exe" if os.name == "nt" else name
        cands += [str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)]
    for c in cands:
        if c and Path(c).exists():
            return str(Path(c).resolve())
    sys.exit("error: perseus-vault binary not found (build it or pass --bin / set PERSEUS_VAULT_BIN).")


class PerseusVault:
    """Persistent MCP stdio client — one process, many calls."""

    def __init__(self, binary, db):
        # Same spawn shape as benchmark/run.py: the binary defaults to MCP
        # stdio when --db is given.
        self.proc = subprocess.Popen(
            [binary, "--db", db],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
        )
        self._id = 0
        init = self._call("initialize", {"protocolVersion": "2025-06-18",
                                         "capabilities": {}, "clientInfo": {"name": "maint-bench"}})
        assert "result" in init or "serverInfo" in str(init)[:400], init
        # Notifications carry NO id and expect NO response — the server
        # would otherwise never answer and the client would block forever.
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                          "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def _call(self, method, params):
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("server exited mid-call")
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def call(self, tool, args):
        resp = self._call("tools/call", {"name": tool, "arguments": args})
        if "error" in resp and resp["error"] is not None:
            raise RuntimeError(f"{tool} failed: {resp['error']}")
        content = resp["result"]["content"]
        text = next(c["text"] for c in content if c.get("type") == "text")
        parsed = json.loads(text)
        if "error" in parsed:
            raise RuntimeError(f"{tool} error: {parsed['error']}")
        return parsed

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


def recall_p95(vault, n=9):
    """p95 of n bounded recall calls (the serving-path latency the gate protects).

    The first call pays any background-embed warmup on the default build, so
    one warmup call precedes the timed samples.
    """
    vault.call("perseus_vault_recall", {"query": "warmup", "limit": 1})
    lat = []
    for i in range(n):
        t0 = time.perf_counter()
        vault.call("perseus_vault_recall", {"query": f"deployment pipeline {i}", "limit": 3})
        lat.append((time.perf_counter() - t0) * 1000.0)
    return statistics.quantiles(lat, n=20)[18] if n >= 20 else sorted(lat)[int(n * 0.95) - 1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin")
    ap.add_argument("--out", default=str(HERE / "report.json"))
    args = ap.parse_args()

    binary = find_binary(args.bin)
    tmp = tempfile.mkdtemp(prefix="perseus-maint-bench-")
    db = os.path.join(tmp, "bench.db")

    t_start = time.perf_counter()
    vault = PerseusVault(binary, db)
    print(f"corpus: {CORPUS_SIZE} entities, {CATEGORIES} categories, seeded")

    # 1. ingest the fixed corpus (measured, reported for context)
    t0 = time.perf_counter()
    for i in range(CORPUS_SIZE):
        cat, body = SEED_TOPICS[i % len(SEED_TOPICS)]
        # 10 near-duplicate variants per topic so consolidation has real groups
        variant = i // len(SEED_TOPICS)
        note = f"{body} variant {variant} note {i}"
        vault.call("perseus_vault_remember", {
            "category": cat, "key": f"bench-{i}",
            "body_json": json.dumps({"note": note}),
            # scoped so the benchmark needs no global authority manifest
            "workspace_hash": "bench-ws",
        })
    ingest_s = time.perf_counter() - t0
    print(f"ingest: {CORPUS_SIZE} entities in {ingest_s:.2f}s")

    # 2. baseline recall latency on the REAL corpus (guard off — no env set)
    pre_p95 = recall_p95(vault)
    print(f"recall p95 pre-run: {pre_p95:.2f} ms")

    # 3. full-budget consolidation (no PERSEUS_VAULT_MAINTENANCE_* env → unguarded).
    # Consolidate scans one category per call; "facts" holds 50 entities with
    # 10 near-duplicate variants — the densest cluster in the corpus.
    t0 = time.perf_counter()
    report = vault.call("perseus_vault_consolidate",
                        {"category": "facts", "limit": 50, "workspace_hash": "bench-ws"})
    el = (time.perf_counter() - t0) * 1000.0
    examined = report.get("entities_examined", 0)
    created = report.get("observations_created", 0)
    guard = report.get("maintenance_guard", {})
    print(f"consolidate: {examined} entities examined in {el:.0f} ms "
          f"({examined / (el / 1000.0):.0f} entities/s), {created} observations")

    # 4. post-run recall latency — the SLO the gate protects
    post_p95 = recall_p95(vault)
    ratio = post_p95 / pre_p95 if pre_p95 else float("inf")
    print(f"recall p95 post-run: {post_p95:.2f} ms (ratio {ratio:.2f}x baseline)")

    vault.close()

    report_out = {
        "corpus": {"entities": CORPUS_SIZE, "categories": CATEGORIES},
        "ingest": {"seconds": round(ingest_s, 3)},
        "consolidate": {
            "entities_examined": examined,
            "elapsed_ms": round(el, 1),
            "entities_per_sec": round(examined / (el / 1000.0), 1),
            "observations_created": created,
            "guard": guard,
        },
        "recall": {"p95_pre_ms": round(pre_p95, 2), "p95_post_ms": round(post_p95, 2),
                   "slo_ratio": round(ratio, 2)},
        "environment": {"binary": binary},
    }
    with open(args.out, "w") as f:
        json.dump(report_out, f, indent=2)
    print(f"\nreport: {args.out}")

    md = (f"| {CORPUS_SIZE} | {examined} | {el/1000:.2f}s | "
          f"{examined/(el/1000.0):.0f} ent/s | {pre_p95:.1f} ms | {post_p95:.1f} ms | "
          f"{ratio:.2f}x |")
    print("\noperating-point row (paste into spec):\n" + md)


if __name__ == "__main__":
    main()
