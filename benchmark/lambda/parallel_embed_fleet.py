#!/usr/bin/env python3
"""parallel_embed_fleet.py — Tier-4 TRUE 8-GPU scale-out throughput.

Client-side round-robin across N pinned Ollama daemons (one per GPU, ports
11434..11434+N-1). No nginx dependency. Measures aggregate embeddings/sec as
concurrency rises, showing how throughput scales when every GPU has its own daemon
(vs the single-daemon ~4.7x saturation).
"""
import json, sys, time, urllib.request, statistics, itertools
from concurrent.futures import ThreadPoolExecutor

NGPU = int(sys.argv[2]) if len(sys.argv) > 2 else 8
OUT = sys.argv[1]
BASE = 11434
MODEL = "nomic-embed-text"
N_PER_LEVEL = 1600
PORTS = [BASE + i for i in range(NGPU)]

def embed(args):
    i, port = args
    body = json.dumps({"model": MODEL,
                       "input": f"benchmark sample {i} about agentic memory context "
                                f"retrieval vector search and recall at scale"}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/embed", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    urllib.request.urlopen(req, timeout=60).read()
    return time.time() - t

def run_level(conc):
    # round-robin request i -> port i % NGPU
    work = [(i, PORTS[i % NGPU]) for i in range(N_PER_LEVEL)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        lats = list(ex.map(embed, work))
    wall = time.time() - t0
    return {"concurrency": conc, "requests": N_PER_LEVEL, "wall_secs": round(wall, 2),
            "aggregate_eps": round(N_PER_LEVEL / wall, 1),
            "p50_req_ms": round(statistics.median(lats) * 1000, 1),
            "p99_req_ms": round(sorted(lats)[int(len(lats)*0.99)] * 1000, 1)}

def main():
    out = {"tier": f"{NGPU}xH100-80GB-fleet", "n_daemons": NGPU, "model": MODEL,
           "arch": "client round-robin across per-GPU pinned Ollama daemons", "levels": []}
    embed((0, PORTS[0]))  # warm
    for conc in (1, 2, 4, 8, 16, 32, 48, 64, 96):
        r = run_level(conc)
        print(json.dumps(r)); out["levels"].append(r)
    base = out["levels"][0]["aggregate_eps"]
    out["peak_eps"] = max(l["aggregate_eps"] for l in out["levels"])
    out["scaling_vs_serial"] = round(out["peak_eps"] / base, 2) if base else None
    open(OUT, "w").write(json.dumps(out, indent=2))
    print(f"\npeak {out['peak_eps']} eps, {out['scaling_vs_serial']}x vs 1-thread -> {OUT}")

if __name__ == "__main__":
    main()
