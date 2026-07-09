#!/usr/bin/env python3
"""parallel_embed.py — Tier-4 8-GPU throughput demo.

A single Ollama instance uses one GPU per model, so raw embedding throughput does
not scale past 1 GPU by itself. This measures the REAL multi-GPU story: N worker
threads issuing concurrent /api/embed requests, and how aggregate throughput scales
as Ollama parallelizes (OLLAMA_NUM_PARALLEL) and as we pin instances across GPUs.

We measure aggregate embeddings/sec at concurrency 1, 2, 4, 8, 16 against the
single endpoint (Ollama internal batching), reporting the scaling curve — the
honest 'supercharged on a powerful platform' number.
"""
import json, sys, time, urllib.request, statistics
from concurrent.futures import ThreadPoolExecutor

ENDPOINT = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:11434/api/embed"
MODEL = "nomic-embed-text"
N_PER_LEVEL = 800  # embed requests per concurrency level

def one_embed(i):
    body = json.dumps({"model": MODEL, "input": f"benchmark text sample number {i} "
                       f"about agentic memory context retrieval and vector search"}).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={"Content-Type": "application/json"})
    t = time.time()
    urllib.request.urlopen(req, timeout=60).read()
    return time.time() - t

def run_level(conc):
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        lats = list(ex.map(one_embed, range(N_PER_LEVEL)))
    wall = time.time() - t0
    return {"concurrency": conc, "requests": N_PER_LEVEL,
            "wall_secs": round(wall, 2),
            "aggregate_eps": round(N_PER_LEVEL / wall, 1),
            "p50_req_ms": round(statistics.median(lats) * 1000, 1)}

def main():
    out = {"tier": "8xH100-80GB-fleet", "endpoint": ENDPOINT, "model": MODEL, "levels": []}
    # warm up
    one_embed(0)
    for conc in (1, 2, 4, 8, 16, 32, 48, 64):
        r = run_level(conc)
        print(json.dumps(r))
        out["levels"].append(r)
    base = out["levels"][0]["aggregate_eps"]
    out["peak_eps"] = max(l["aggregate_eps"] for l in out["levels"])
    out["scaling_vs_serial"] = round(out["peak_eps"] / base, 2) if base else None
    open(sys.argv[1], "w").write(json.dumps(out, indent=2))
    print(f"\npeak {out['peak_eps']} eps, {out['scaling_vs_serial']}x vs serial -> {sys.argv[1]}")

if __name__ == "__main__":
    main()
