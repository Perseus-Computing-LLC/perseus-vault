#!/usr/bin/env python3
"""Live serving contention + agent-economics benchmark.

Runs against the REAL perseus-vault binary (MCP stdio) plus any
OpenAI-compatible chat endpoint (vLLM, llama.cpp, TGI, ...) on the same host,
and measures three things:

  1. LLM serving throughput — output tokens/sec under sustained concurrent
     load (the "accelerator is busy serving tokens" state).
  2. Recall latency on the HOST CPU, measured BOTH while the endpoint is idle
     AND while it is saturated by (1), with the identical probe list. The
     load-bearing claim is that the CPU memory layer steals no inference
     cycles — if recall p50 barely moves, that claim is MEASURED.
  3. Economics: $/1M output tokens and $/agent-hour, derived from the measured
     throughput, the concurrent-agent ceiling, and the GPU's real hourly price.

Every line printed under [measured] is timed live on the box you run it on.
The concurrent-agent ceiling is MEASURED when you pass --vllm-log (vLLM
reports its KV-cache-bound "Maximum concurrency for N tokens per request: X"
at startup); otherwise it falls back to published-spec HBM math and is
labelled [derived].

On the accelerator host (vLLM already serving on :8000):
    python benchmark/contention/live_bench.py \
        --base-url http://127.0.0.1:8000 --model Qwen/Qwen2.5-72B-Instruct \
        --gpu-price 2.19 --vllm-log /path/to/vllm.log \
        --concurrency 32 --duration 60

Dry-run on a laptop against llama.cpp first to validate the harness:
    llama-server -m some-model.gguf --port 8081 &
    python benchmark/contention/live_bench.py --base-url http://127.0.0.1:8081 \
        --model some-model --concurrency 4 --duration 15 --store-size 1000
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vault_client import (Vault, find_binary, fresh_db, host_cpu,
                          make_probes, measure_recall, seed_store)

PROMPT = ("You are a helpful assistant. In two or three sentences, explain why "
          "keeping an AI agent's long-term memory off the GPU frees HBM for "
          "serving tokens. Be concrete about the tradeoff.")


def _one_completion(base_url: str, model: str, max_tokens: int,
                    api_key: str) -> "tuple[int, float]":
    """One chat completion. Returns (completion_tokens, seconds); (0, dt) on error."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens, "temperature": 0.7,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(f"{base_url}/v1/chat/completions",
                                 data=body, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        dt = time.perf_counter() - t0
        return int(d.get("usage", {}).get("completion_tokens", 0)), dt
    except Exception:
        return 0, time.perf_counter() - t0


def load_generator(base_url, model, max_tokens, api_key, concurrency,
                   stop: threading.Event, stats: dict) -> None:
    """Keep `concurrency` completions in flight until `stop` is set."""
    def worker():
        while not stop.is_set():
            toks, _dt = _one_completion(base_url, model, max_tokens, api_key)
            with stats["lock"]:
                stats["tokens"] += toks
                stats["reqs"] += 1
                if toks == 0:
                    stats["errors"] += 1
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for _ in range(concurrency):
            ex.submit(worker)
        stop.wait()


def measured_concurrency_from_vllm(log_path: str) -> "float | None":
    """Read vLLM's KV-cache-bound max concurrency from its startup log.

    vLLM logs e.g. 'Maximum concurrency for 8192 tokens per request: 20.4x'.
    That is the accelerator's real concurrent-sequence ceiling given
    (HBM - weights - overhead) / measured-KV-per-seq — a MEASURED capacity,
    not a published-spec estimate. Returns the number, or None if not found.
    """
    try:
        with open(log_path, errors="ignore") as f:
            text = f.read()
    except OSError:
        return None
    m = re.findall(r"[Mm]aximum concurrency.*?:\s*([0-9]+(?:\.[0-9]+)?)", text)
    return float(m[-1]) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bin", default=None, help="path to the perseus-vault binary")
    ap.add_argument("--base-url", default=os.environ.get("BASE_URL",
                                                         "http://127.0.0.1:8000"))
    ap.add_argument("--model", default=os.environ.get("MODEL", ""))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--duration", type=float, default=60.0, help="load seconds")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--recall-iters", type=int, default=500,
                    help="recalls per pass (each ~15-25 ms at 100K)")
    ap.add_argument("--store-size", type=int, default=100_000)
    ap.add_argument("--mode", default="fts5", choices=["fts5", "dense", "hybrid"])
    ap.add_argument("--gpu-price", type=float, default=0.0,
                    help="$/GPU-hr actually paid; 0 skips the economics rows")
    ap.add_argument("--gpu-hbm-gb", type=float, default=0.0,
                    help="HBM GB (published spec) for the derived agent ceiling")
    ap.add_argument("--model-weights-gb", type=float, default=0.0)
    ap.add_argument("--ctx-tokens", type=int, default=8192)
    ap.add_argument("--kv-gb-per-seq", type=float, default=2.5,
                    help="published-spec KV cache per --ctx-tokens sequence")
    ap.add_argument("--vllm-log", default="",
                    help="vLLM server log path; if given, the MEASURED "
                         "concurrent-sequence ceiling is read from its "
                         "'Maximum concurrency ...' startup line")
    ap.add_argument("--out", default=str(Path(tempfile.gettempdir()) /
                                         "contention-live-report.json"))
    args = ap.parse_args()

    binary = find_binary(args.bin)
    cpu = host_cpu()
    print("=" * 74)
    print("Perseus Vault live contention benchmark (memory on CPU, model on GPU)")
    print("=" * 74)
    print(f"Binary       : {binary}")
    print(f"Host CPU     : {cpu}")
    print(f"LLM endpoint : {args.base_url}  model={args.model or '(unset)'}")
    print(f"Store size   : {args.store_size:,} entities (real binary, MCP stdio)")
    print()

    db = fresh_db("vault-contention-live.db")
    v = Vault(binary, db)
    try:
        print(f"Seeding {args.store_size:,} entities...", flush=True)
        seed_store(v, args.store_size)
        probes = make_probes(args.store_size)
        measure_recall(v, probes, min(50, args.recall_iters), args.mode)  # warm

        # --- (A) Recall with the endpoint idle. ------------------------------
        idle = measure_recall(v, probes, args.recall_iters, args.mode)
        print(f"[measured] recall @ endpoint idle  : p50 {idle['p50_ms']} ms  "
              f"p99 {idle['p99_ms']} ms  ({idle['ops_s']:,} ops/s)")

        # --- (B) Saturate the endpoint, re-measure recall. -------------------
        stop = threading.Event()
        lstats = {"tokens": 0, "reqs": 0, "errors": 0, "lock": threading.Lock()}
        gen = threading.Thread(target=load_generator, args=(
            args.base_url, args.model, args.max_tokens, args.api_key,
            args.concurrency, stop, lstats), daemon=True)
        t_load0 = time.perf_counter()
        gen.start()
        time.sleep(min(5.0, args.duration / 4))  # let the load reach steady state

        busy = measure_recall(v, probes, args.recall_iters, args.mode)
        remaining = args.duration - (time.perf_counter() - t_load0)
        if remaining > 0:
            time.sleep(remaining)
        stop.set()
        gen.join(timeout=200)
        load_wall = time.perf_counter() - t_load0

        tok_s = lstats["tokens"] / load_wall if load_wall else 0.0
        print(f"[measured] recall @ endpoint loaded: p50 {busy['p50_ms']} ms  "
              f"p99 {busy['p99_ms']} ms  ({busy['ops_s']:,} ops/s)")
        delta = busy["p50_ms"] - idle["p50_ms"]
        pct = (delta / idle["p50_ms"] * 100.0) if idle["p50_ms"] else 0.0
        print(f"           -> recall p50 moved {delta:+.3f} ms ({pct:+.1f}%) "
              f"under serving load")
        print()

        # --- (C) Serving throughput + economics. -----------------------------
        print(f"[measured] serving throughput      : {tok_s:,.1f} output tok/s "
              f"@ concurrency {args.concurrency} ({lstats['reqs']} reqs, "
              f"{lstats['errors']} errors, {load_wall:.1f}s)")
        usd_per_mtok = None
        if tok_s > 0 and args.gpu_price > 0:
            usd_per_mtok = args.gpu_price / (tok_s * 3600) * 1e6
            print(f"[measured] $ / 1M output tokens    : ${usd_per_mtok:.3f} "
                  f"(= ${args.gpu_price}/hr / {tok_s:,.0f} tok/s)")

        # Concurrent-agent ceiling: prefer vLLM's own KV-cache-bound number
        # (MEASURED on this accelerator); else published-spec HBM math (derived).
        measured_agents = (measured_concurrency_from_vllm(args.vllm_log)
                           if args.vllm_log else None)
        agents, per_agent = None, None
        if measured_agents:
            agents, tag = measured_agents, "measured"
            how = f"vLLM KV-cache ceiling @ {args.ctx_tokens}-token ctx"
        elif args.gpu_hbm_gb > 0 and args.model_weights_gb > 0:
            free = args.gpu_hbm_gb - args.model_weights_gb
            agents, tag = max(0.0, free / args.kv_gb_per_seq), "derived"
            how = (f"({args.gpu_hbm_gb:g}-{args.model_weights_gb:g} GB free)/"
                   f"{args.kv_gb_per_seq:g} GB/seq, published-spec HBM math")
        else:
            tag, how = "skipped", "pass --vllm-log or --gpu-hbm-gb/--model-weights-gb"
        if agents:
            print(f"[{tag}]  concurrent agents        : {agents:.1f}  ({how})")
            if args.gpu_price > 0:
                per_agent = args.gpu_price / agents
                print(f"[{tag}]  GPU $ / agent-hour       : ${per_agent:.3f}  "
                      f"(measured GPU ${args.gpu_price}/hr / {tag} ceiling)")
        else:
            print(f"[{tag}]  concurrent agents        : {how}")

        report = {
            "benchmark": "contention-live", "binary": Path(binary).name,
            "platform": platform.platform(), "host_cpu": cpu,
            "endpoint": args.base_url, "model": args.model,
            "store_size": args.store_size, "mode": args.mode,
            "recall_iters": args.recall_iters,
            "recall_idle": {**idle, "data_source": "measured"},
            "recall_loaded": {**busy, "data_source": "measured"},
            "p50_delta_ms": round(delta, 3), "p50_delta_pct": round(pct, 1),
            "serving": {"output_tok_s": round(tok_s, 1),
                        "concurrency": args.concurrency,
                        "requests": lstats["reqs"], "errors": lstats["errors"],
                        "wall_s": round(load_wall, 1),
                        "data_source": "measured"},
            "economics": {
                "gpu_price_hr": args.gpu_price or None,
                "usd_per_1m_output_tokens":
                    round(usd_per_mtok, 3) if usd_per_mtok else None,
                "concurrent_agents": round(agents, 1) if agents else None,
                "usd_per_agent_hr": round(per_agent, 3) if per_agent else None,
                "agents_data_source": tag,
            },
        }
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n",
                                  encoding="utf-8")
        print(f"\nreport -> {args.out}")
        return 0
    finally:
        v.close()


if __name__ == "__main__":
    sys.exit(main())
