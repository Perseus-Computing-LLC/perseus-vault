"""Shared plumbing for the GPU-contention benchmarks (#530).

A persistent MCP stdio client for the real perseus-vault binary (one process,
many calls — the same shape as ``benchmark/scale/run.py``), plus corpus seeding
and a recall-latency probe. Pure stdlib; no third-party deps.
"""
from __future__ import annotations

import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

# Pseudo-prose vocabulary (same family as benchmark/scale): bodies must be
# realistic in size and unique enough that near-duplicate merging doesn't
# collapse the seed corpus. We also pass skip_dedup (#531) — this is exactly
# the templated-bulk-ingest case it exists for.
WORDS = ("service latency schema deploy index queue worker retry cache shard "
         "vault memory recall decay layer topic entity journal replica merge "
         "postgres sqlite onnx embedding vector keyword hybrid ranking fusion "
         "timeout breaker fallback config workspace scope audit chain commit "
         "release binary stdio transport tool argument budget threshold gate").split()


def find_binary(explicit: "str | None" = None) -> str:
    cands = [explicit, os.environ.get("MIMIR_BIN")]
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        cands += [str(REPO / "target" / "release" / exe),
                  str(REPO / "target" / "debug" / exe)]
    for c in cands:
        if c and Path(c).exists():
            return str(Path(c).resolve())
    sys.exit("error: perseus-vault binary not found. Build it "
             "(`cargo build --release`) or pass --bin / set MIMIR_BIN.")


class Vault:
    """Persistent MCP stdio client — one process, many calls."""

    def __init__(self, binary: str, db: str):
        self.p = subprocess.Popen([binary, "--db", db], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace")
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": self._n(), "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "contention-bench", "version": "1.0"}}})
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _n(self):
        self._id += 1
        return self._id

    def _send(self, m):
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()

    def _read(self):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("perseus-vault closed the stream")
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "result" in m or "error" in m:
                return m

    def call(self, name, args=None):
        self._send({"jsonrpc": "2.0", "id": self._n(), "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}})
        resp = self._read()
        r = resp.get("result", {})
        if isinstance(r, dict) and "content" in r:
            try:
                return json.loads(r["content"][0]["text"])
            except Exception:
                return r["content"][0]["text"]
        return resp

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=60)
        except Exception:
            self.p.kill()


def fresh_db(name: str) -> str:
    import tempfile
    db = str(Path(tempfile.gettempdir()) / name)
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass
    return db


def seed_store(v: Vault, n: int, seed: int = 530) -> None:
    """Bulk-load n distinct entities (skip_dedup — see #531)."""
    rng = random.Random(seed)
    cats = ["decision", "architecture", "convention", "insight",
            "fact", "infrastructure", "reference", "conversation"]
    tenth = max(1, n // 10)
    t0 = time.perf_counter()
    for i in range(n):
        words = " ".join(rng.choices(WORDS, k=rng.randint(40, 120)))
        v.call("mimir_remember", {
            "category": cats[i % len(cats)],
            "key": f"contention-{i}",
            "body_json": json.dumps({"text": f"[{i}] {words}",
                                     "detail": f"entity {i} in group {i % 977}"}),
            "type": "insight",
            "skip_dedup": True,
        })
        if (i + 1) % tenth == 0:
            rate = (i + 1) / (time.perf_counter() - t0)
            print(f"  seeded {i + 1:,}/{n:,} ({rate:,.0f}/s)", flush=True)


def make_probes(n_entities: int, count: int = 32, seed: int = 531) -> list:
    """A fixed probe-query list, reused verbatim for the idle and the loaded
    pass so the two measurements are directly comparable."""
    rng = random.Random(seed)
    return [f"entity {rng.randrange(n_entities)} group {rng.randrange(977)}"
            for _ in range(count)]


def measure_recall(v: Vault, probes: list, iters: int, mode: str = "fts5") -> dict:
    """Time `iters` recalls against the real binary. p50/p99/mean ms + ops/s."""
    lat = []
    t0 = time.perf_counter()
    for i in range(iters):
        s = time.perf_counter()
        v.call("mimir_recall", {"query": probes[i % len(probes)],
                                "mode": mode, "limit": 5})
        lat.append((time.perf_counter() - s) * 1000.0)
    wall = time.perf_counter() - t0
    lat.sort()
    return {
        "p50_ms": round(lat[len(lat) // 2], 3),
        "p99_ms": round(lat[min(len(lat) - 1, int(len(lat) * 0.99))], 3),
        "mean_ms": round(statistics.fmean(lat), 3),
        "ops_s": round(iters / wall, 1) if wall else 0.0,
    }


def host_cpu() -> str:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor).Name"],
                capture_output=True, text=True, timeout=15).stdout.strip()
            if out:
                return out
        except Exception:
            pass
    return os.environ.get("PROCESSOR_IDENTIFIER", "unknown CPU")
