#!/usr/bin/env python3
"""Synthetic GPU-burn contention benchmark — does recall steal accelerator cycles?

Question: does Perseus Vault's CPU-side recall path contend with a saturated
accelerator (or, on a CPU-only box, with saturated cores)? Method: measure
recall latency against the REAL binary over MCP stdio, then drive the
accelerator to 100% with a compute-bound FP16 matmul — isolated in
subprocesses so the burn can't contend with the measurement for the Python
GIL — and measure recall again with the identical probe list. If recall p50
barely moves, the memory layer and the accelerator do not contend.

The burn is vendor-neutral: torch's ``cuda`` device covers both CUDA and ROCm
builds (an MI300X shows up as ``torch.cuda``). If torch is missing or no GPU
is present, each burner falls back to a pure-Python CPU spin (one saturated
core per burner, clearly labelled) so the harness runs end-to-end anywhere.

Measured on a rented AMD Instinct MI300X node (RunPod, ~192-core AMD EPYC
host, 2026-07-09): recall p50 moved +0.6% between GPU idle and GPU 100%
(97.4 TFLOPS FP16 sustained). See PERF.md (#530).

Usage:
    cargo build --release
    python benchmark/contention/burn_bench.py --store-size 100000
    python benchmark/contention/burn_bench.py --bin /path/to/perseus-vault \
        --burners 3 --store-size 100000 --iters 3000
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from vault_client import (Vault, find_binary, fresh_db, host_cpu,
                          make_probes, measure_recall, seed_store)

# Self-contained burner: one process pins a chunk of the accelerator with FP16
# matmuls (argv[1] = throughput report file). Without torch/GPU it degrades to
# a pure-Python spin that saturates one CPU core.
BURN_SRC = """\
import sys, time
out = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    import torch
    gpu = torch.cuda.is_available()
    d = "cuda" if gpu else "cpu"
    S = 8192 if gpu else 1024
    dt = torch.float16 if gpu else torch.float32
    a = torch.randn(S, S, device=d, dtype=dt)
    b = torch.randn(S, S, device=d, dtype=dt)
    n = 0; t0 = time.perf_counter()
    while True:
        c = a @ b
        if gpu:
            torch.cuda.synchronize()
        n += 1
        if out and n % (200 if gpu else 20) == 0:
            tf = (2 * S ** 3 * n) / (time.perf_counter() - t0) / 1e12
            open(out, "w").write(f"{'gpu' if gpu else 'cpu-torch'} {tf:.1f}")
except ImportError:
    if out:
        open(out, "w").write("cpu-spin 0")
    x = 1.0
    while True:  # pure-Python spin: saturates one core, no deps
        x = x * 1.0000001 + 1e-9
        if x > 1e6:
            x = 1.0
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", default=None, help="path to the perseus-vault binary")
    ap.add_argument("--burners", type=int, default=3,
                    help="parallel burn processes (GPU: 3 is plenty; "
                         "CPU-spin fallback: one core each, so use ~cores)")
    ap.add_argument("--store-size", type=int, default=100_000)
    ap.add_argument("--iters", type=int, default=1000, help="recalls per pass")
    ap.add_argument("--ramp", type=float, default=15.0,
                    help="seconds to let the burn ramp before the loaded pass")
    ap.add_argument("--mode", default="fts5", choices=["fts5", "dense", "hybrid"],
                    help="recall mode (dense/hybrid need the bundled-embeddings "
                         "build plus a mimir_embed pass; default fts5)")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) /
                                         "contention-burn-report.json"))
    args = ap.parse_args()

    binary = find_binary(args.bin)
    dev = "torch unavailable"
    have_gpu = False
    try:
        import torch
        have_gpu = torch.cuda.is_available()
        dev = torch.cuda.get_device_name(0) if have_gpu else "no GPU (torch present)"
    except Exception:
        pass

    print(f"Binary           : {binary}")
    print(f"Host CPU         : {host_cpu()}  ({os.cpu_count()} logical)")
    print(f"Accelerator      : {dev}")

    db = fresh_db("vault-contention-burn.db")
    v = Vault(binary, db)
    tmpdir = tempfile.mkdtemp(prefix="vault-burn-")
    tput = os.path.join(tmpdir, "burn_tput.txt")
    burn_file = os.path.join(tmpdir, "_burn.py")
    procs = []
    try:
        print(f"Seeding {args.store_size:,} entities...", flush=True)
        seed_store(v, args.store_size)
        probes = make_probes(args.store_size)
        measure_recall(v, probes, min(50, args.iters), args.mode)  # warm

        idle = measure_recall(v, probes, args.iters, args.mode)
        print(f"[measured] recall @ idle      : p50 {idle['p50_ms']:.3f} ms  "
              f"p99 {idle['p99_ms']:.3f} ms  ({idle['ops_s']:,} ops/s)")

        with open(burn_file, "w") as f:
            f.write(BURN_SRC)
        procs = [subprocess.Popen([sys.executable, burn_file, tput],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
                 for _ in range(args.burners)]
        time.sleep(args.ramp)

        kind, tf = "cpu-spin", ""
        try:
            kind, tf = open(tput).read().split()
        except (OSError, ValueError):
            pass
        label = {"gpu": f"GPU 100%, {tf} TFLOPS FP16",
                 "cpu-torch": f"CPU torch matmul, {tf} TFLOPS FP32",
                 "cpu-spin": f"CPU spin x{args.burners} (no torch/GPU)"}[kind]

        busy = measure_recall(v, probes, args.iters, args.mode)
        print(f"[measured] recall @ saturated : p50 {busy['p50_ms']:.3f} ms  "
              f"p99 {busy['p99_ms']:.3f} ms  ({label})")
        d = busy["p50_ms"] - idle["p50_ms"]
        pct = (d / idle["p50_ms"] * 100.0) if idle["p50_ms"] else 0.0
        print(f"           -> recall p50 moved {d:+.3f} ms ({pct:+.1f}%) under load")
        if kind == "gpu":
            print("           -> CPU memory layer and accelerator do not contend "
                  "(0 bytes of GPU HBM used by memory)")

        report = {
            "benchmark": "contention-burn", "data_source": "measured",
            "binary": Path(binary).name, "platform": platform.platform(),
            "host_cpu": host_cpu(), "accelerator": dev, "load": label,
            "store_size": args.store_size, "iters": args.iters,
            "mode": args.mode, "burners": args.burners,
            "recall_idle": idle, "recall_saturated": busy,
            "p50_delta_ms": round(d, 3), "p50_delta_pct": round(pct, 1),
        }
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"report -> {args.out}")
        return 0
    finally:
        for p in procs:
            p.terminate()
        v.close()
        for f in (burn_file, tput):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
