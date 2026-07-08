#!/usr/bin/env python3
"""Perseus Vault scale benchmark — 10K / 100K / 1M entities with latency budgets (#474).

Drives the REAL binary over MCP stdio (one persistent process per corpus size)
and measures, at each size N:

  - sustained write throughput (entities/s), plus first-vs-last-10% throughput
    so non-linear degradation is visible, not averaged away
  - batch embed throughput (bundled ONNX, no network) — the dense-index build cost
  - recall latency p50/p95/p99 for fts5 / dense / hybrid modes
  - bi-temporal point-lookup latency (`mimir_as_of`) and transaction-time
    reconstruction recall (`as_of_unix_ms`) — the differentiator must stay fast
  - DB + index size on disk
  - cold start: process spawn + initialize + first recall, on the loaded DB

Fully offline and deterministic (seeded corpus). Emits a signed report with
named hardware, binary version, and commit SHA:

    python benchmark/scale/run.py                        # 10K + 100K
    python benchmark/scale/run.py --sizes 10000          # quick
    python benchmark/scale/run.py --sizes 10000 100000 1000000   # incl. 1M (manual/nightly)
    python benchmark/scale/run.py --skip-embed           # fts5/bitemporal only (no dense build)

The curated benchmark/scale/report.json is a committed artifact; raw runs default
to OS temp — pass --out to capture. Budgets live in gate.py (see README.md).
"""
import argparse
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

CATEGORIES = ["decision", "architecture", "convention", "insight", "fact",
              "infrastructure", "reference", "conversation"]

# Deterministic pseudo-prose vocabulary: bodies must be realistic in size and
# unique enough that near-duplicate detection doesn't collapse the bulk load
# into dedup rejections (that would measure dedup, not inserts).
WORDS = ("service latency schema deploy index queue worker retry cache shard "
         "vault memory recall decay layer topic entity journal replica merge "
         "postgres sqlite onnx embedding vector keyword hybrid ranking fusion "
         "timeout breaker fallback config workspace scope audit chain commit "
         "release binary stdio transport tool argument budget threshold gate").split()


def find_binary(explicit):
    cands = [explicit, os.environ.get("MIMIR_BIN")]
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        cands += [str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)]
    for c in cands:
        if c and Path(c).exists():
            return str(Path(c).resolve())
    sys.exit("error: perseus-vault binary not found (build it or pass --bin / set MIMIR_BIN).")


class Vault:
    """Persistent MCP stdio client — one process, many calls."""

    def __init__(self, binary, db):
        self.p = subprocess.Popen([binary, "--db", db], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace")
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": self._n(), "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "scale-bench", "version": "1.0"}}})
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


def body_for(i: int, rng: random.Random) -> str:
    """Realistic body: 40-120 pseudo-prose words + a unique nonce."""
    n = rng.randint(40, 120)
    words = rng.choices(WORDS, k=n)
    nonce = hashlib.sha1(f"scale-{i}".encode()).hexdigest()[:16]
    return json.dumps({
        "text": f"[{i}] " + " ".join(words),
        "detail": f"entity {i} in group {i % 977}",
        "nonce": nonce,
    })


def pctl(sorted_ms, q):
    if not sorted_ms:
        return None
    idx = min(len(sorted_ms) - 1, max(0, int(round(q * (len(sorted_ms) - 1)))))
    return round(sorted_ms[idx], 2)


def lat_summary(times_ms):
    ts = sorted(times_ms)
    return {"p50_ms": pctl(ts, 0.50), "p95_ms": pctl(ts, 0.95),
            "p99_ms": pctl(ts, 0.99), "avg_ms": round(statistics.mean(ts), 2),
            "n": len(ts)}


def run_size(binary, n, queries, skip_embed, keep_db=False):
    rng = random.Random(474)  # deterministic corpus per size
    db = str(Path(tempfile.gettempdir()) / f"vault-scale-{n}.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass

    out = {"entities": n}
    v = Vault(binary, db)
    try:
        # ── 1. Bulk load ──
        print(f"[{n:,}] loading...", flush=True)
        tenth = max(1, n // 10)
        t0 = time.perf_counter()
        t_first = t_last = None
        as_of_probe_ms = None
        for i in range(n):
            v.call("mimir_remember", {
                "category": CATEGORIES[i % len(CATEGORIES)],
                "key": f"scale-{i}",
                "body_json": body_for(i, rng),
                "type": "insight",
            })
            if i + 1 == tenth:
                t_first = time.perf_counter() - t0
            if i + 1 == n - tenth:
                t_last = time.perf_counter()
            if i == 0:
                # Transaction-time instant AFTER the first write — the as_of
                # reconstruction probe target (the first entity's first version).
                as_of_probe_ms = int(time.time() * 1000) + 1
        elapsed = time.perf_counter() - t0
        last_tenth_s = (time.perf_counter() - t_last) if t_last else None
        out["write"] = {
            "count": n, "elapsed_s": round(elapsed, 1),
            "docs_per_sec": round(n / elapsed),
            "first_10pct_docs_per_sec": round(tenth / t_first) if t_first else None,
            "last_10pct_docs_per_sec": round(tenth / last_tenth_s) if last_tenth_s else None,
        }
        print(f"[{n:,}] write: {out['write']['docs_per_sec']}/s "
              f"(first 10%: {out['write']['first_10pct_docs_per_sec']}/s, "
              f"last 10%: {out['write']['last_10pct_docs_per_sec']}/s)", flush=True)

        # ── 2. Dense index build (bundled ONNX; the cost of semantic search) ──
        modes = ["fts5"]
        if not skip_embed:
            print(f"[{n:,}] embedding...", flush=True)
            t0 = time.perf_counter()
            embedded = 0
            for cat in CATEGORIES:
                while True:
                    rep = v.call("mimir_embed", {"batch_category": cat, "batch_limit": 5000})
                    got = int(rep.get("embedded", 0) or 0) if isinstance(rep, dict) else 0
                    embedded += got
                    if got < 5000:
                        break
            el = time.perf_counter() - t0
            out["embed"] = {"embedded": embedded, "elapsed_s": round(el, 1),
                            "embeds_per_sec": round(embedded / el) if el > 0 else None}
            print(f"[{n:,}] embed: {embedded} in {el:.0f}s", flush=True)
            if embedded:
                modes += ["dense", "hybrid"]

        # ── 3. Recall latency per mode ──
        out["recall"] = {}
        for mode in modes:
            times = []
            for q in range(queries):
                term = f"entity {rng.randrange(n)} group {rng.randrange(977)}"
                t0 = time.perf_counter()
                v.call("mimir_recall", {"query": term, "mode": mode, "limit": 10})
                times.append((time.perf_counter() - t0) * 1000)
            out["recall"][mode] = lat_summary(times)
            print(f"[{n:,}] recall {mode}: p50={out['recall'][mode]['p50_ms']}ms "
                  f"p99={out['recall'][mode]['p99_ms']}ms", flush=True)

        # ── 4. Bi-temporal at scale ──
        times = []
        for q in range(queries):
            i = rng.randrange(n)
            t0 = time.perf_counter()
            v.call("mimir_as_of", {"category": CATEGORIES[i % len(CATEGORIES)],
                                   "key": f"scale-{i}",
                                   "as_of_unix_ms": as_of_probe_ms + 10_000_000_000})
            times.append((time.perf_counter() - t0) * 1000)
        out["as_of"] = lat_summary(times)
        times = []
        for q in range(queries):
            term = f"entity {rng.randrange(n)}"
            t0 = time.perf_counter()
            v.call("mimir_recall", {"query": term, "mode": "fts5", "limit": 10,
                                    "as_of_unix_ms": as_of_probe_ms + 10_000_000_000})
            times.append((time.perf_counter() - t0) * 1000)
        out["temporal_recall"] = lat_summary(times)
        print(f"[{n:,}] as_of: p50={out['as_of']['p50_ms']}ms · "
              f"temporal recall p50={out['temporal_recall']['p50_ms']}ms", flush=True)

        # ── 5. Size + integrity ──
        stats = v.call("mimir_stats", {})
        out["db"] = {"file_bytes": stats.get("db_file_size_bytes"),
                     "file_mb": round((stats.get("db_file_size_bytes") or 0) / 1048576, 1),
                     "active_entities": stats.get("active_entities",
                                                  stats.get("total_entities"))}
    finally:
        v.close()

    # ── 6. Cold start on the loaded DB: spawn + initialize + first recall ──
    trials = []
    for _ in range(3):
        t0 = time.perf_counter()
        v2 = Vault(binary, db)
        v2.call("mimir_recall", {"query": "entity 1 group 1", "mode": "fts5", "limit": 5})
        trials.append((time.perf_counter() - t0) * 1000)
        v2.close()
    out["cold_start"] = {"first_query_ms_min": round(min(trials), 1),
                         "first_query_ms_median": round(statistics.median(trials), 1)}
    print(f"[{n:,}] cold start: {out['cold_start']['first_query_ms_median']}ms", flush=True)

    if not keep_db:
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db + ext)
            except OSError:
                pass
    return out


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                              text=True, timeout=10).stdout.strip()[:12] or None
    except Exception:
        return None


def binary_version(binary):
    try:
        return subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip() or None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Perseus Vault scale benchmark (#474)")
    ap.add_argument("--bin", default=None)
    ap.add_argument("--sizes", nargs="+", type=int, default=[10_000, 100_000])
    ap.add_argument("--queries", type=int, default=100, help="Latency samples per metric")
    ap.add_argument("--skip-embed", action="store_true",
                    help="Skip the dense-index build (fts5 + bitemporal only)")
    ap.add_argument("--keep-db", action="store_true", help="Keep the loaded DBs in temp")
    # Curated report.json is a committed artifact — raw runs default to OS temp.
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "vault-scale-report.json"))
    args = ap.parse_args()

    binary = find_binary(args.bin)
    report = {
        "benchmark": "scale",
        "issue": "#474",
        "meta": {
            "binary": binary_version(binary),
            "commit": git_commit(),
            "hardware": {"machine": platform.machine(), "processor": platform.processor(),
                         "cpus": os.cpu_count(), "os": f"{platform.system()} {platform.release()}",
                         "python": platform.python_version()},
            "queries_per_metric": args.queries,
            "transport": "MCP stdio, one persistent process per corpus size",
        },
        "runs": {},
    }
    for n in sorted(set(args.sizes)):
        report["runs"][str(n)] = run_size(binary, n, args.queries, args.skip_embed,
                                          keep_db=args.keep_db)

    # Sign the run payload so the published page can verify provenance.
    payload = json.dumps(report["runs"], sort_keys=True).encode()
    report["signature_sha256"] = hashlib.sha256(payload).hexdigest()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved: {args.out}")


if __name__ == "__main__":
    main()
