#!/usr/bin/env python3
"""quality_lift.py — WS4: does a bigger model make mimir_ask meaningfully better?

Runs the SAME question set through mimir_ask twice (14B vs 72B chat model), over
the SAME seeded+embedded vault, and reports per-model accuracy (expected-substring
present), citation presence, and answer latency. This quantifies the
"supercharged when leveraged on a powerful platform" claim honestly.

Embeddings are identical across both runs (nomic-embed-text via --embedding-model-name,
the #525 fix), so ONLY the generation model changes — a clean A/B.
"""
import argparse, json, time, sys, os, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rag_bench import MCP

# Fact base + questions needing synthesis/grounding (not just keyword lookup).
FACTS = [
    ("Perseus Vault stores memories in SQLite with FTS5 and optional dense embeddings via Ollama."),
    ("The vault runs fully air-gapped: --offline disables all network calls for IL5/ICD-503 use."),
    ("Bi-temporal versioning tracks both transaction time (when recorded) and valid time (when true)."),
    ("Ebbinghaus decay fades unused memories; consolidation merges cold ones into durable insights."),
    ("Hybrid recall fuses dense vector search with FTS5 keyword search via reciprocal rank fusion."),
    ("Entities are encrypted at rest with AES-256-GCM, keyed per workspace."),
]
QUESTIONS = [
    ("Can Perseus Vault work without any internet connection, and how?", ["offline", "air-gap"]),
    ("How does the vault answer 'what did we believe last week' versus 'what was true last week'?",
     ["transaction", "valid"]),
    ("What happens to memories that are never accessed?", ["decay", "fade", "consolidat"]),
    ("How does the vault combine keyword and semantic search?", ["hybrid", "fusion", "rank"]),
    ("Is stored data protected if the database file is stolen?", ["encrypt", "aes"]),
]

def judge(ans, keys):
    a = (ans or "").lower()
    return any(k.lower() in a for k in keys)

def run_model(bin_path, db, chat_model, embed_endpoint, gen_endpoint):
    argv = [bin_path, "serve", "--db", db,
            "--llm-endpoint", gen_endpoint, "--llm-model", chat_model,
            "--embedding-endpoint", embed_endpoint,
            "--embedding-model-name", "nomic-embed-text"]  # #525 fix
    m = MCP(argv)
    res = {"model": chat_model, "probes": []}
    try:
        # Pre-warm the chat model so the first timed mimir_ask doesn't eat the
        # cold-load penalty (a 72B takes >30s to load into VRAM, which exceeds the
        # vault's hardcoded 30s LLM timeout and confounds the quality comparison).
        # This isolates ANSWER QUALITY from model-load time.
        import urllib.request as _u
        try:
            _b = json.dumps({"model": chat_model, "prompt": "hi", "stream": False,
                             "keep_alive": "30m"}).encode()
            _r = _u.Request(gen_endpoint, data=_b, headers={"Content-Type": "application/json"})
            _u.urlopen(_r, timeout=600).read()
        except Exception as e:
            print(f"warmup warning ({chat_model}): {e}")
        for i, f in enumerate(FACTS):
            m.tool("mimir_remember", {"category": "kb", "key": f"f{i}",
                   "body_json": json.dumps({"content": f})})
        # embed once (idempotent per model DB)
        while True:
            e = m.tool("mimir_embed", {"batch_category": "kb", "batch_limit": 1000})
            if (e.get("embedded", 0) or 0) == 0:
                break
        for q, keys in QUESTIONS:
            t = time.time()
            a = m.tool("mimir_ask", {"query": q, "top_k": 5})
            dt = time.time() - t
            ans = a.get("answer", str(a)) if isinstance(a, dict) else str(a)
            srcs = a.get("sources", []) if isinstance(a, dict) else []
            res["probes"].append({"q": q, "correct": judge(ans, keys),
                                  "has_citation": bool(srcs),
                                  "latency_s": round(dt, 2), "answer": ans[:280]})
    finally:
        m.close()
    n = len(res["probes"])
    res["summary"] = {
        "accuracy": round(sum(p["correct"] for p in res["probes"]) / n, 3),
        "citation_rate": round(sum(p["has_citation"] for p in res["probes"]) / n, 3),
        "mean_latency_s": round(statistics.mean(p["latency_s"] for p in res["probes"]), 2),
        "p50_latency_s": round(statistics.median(p["latency_s"] for p in res["probes"]), 2),
    }
    return res

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    ap.add_argument("--gen-endpoint", default="http://localhost:11434/api/generate")
    ap.add_argument("--embed-endpoint", default="http://localhost:11434/api/embed")
    ap.add_argument("--db-prefix", default="/tmp/quality")
    ap.add_argument("--models", nargs="+", default=["qwen2.5:14b-instruct", "qwen2.5:72b-instruct"])
    ap.add_argument("--tier", default="2xH100-80GB")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    report = {"tier": a.tier, "task": "mimir_ask grounded QA", "models": []}
    for mdl in a.models:
        db = f"{a.db_prefix}_{mdl.replace(':','_').replace('.','')}.db"
        os.system(f"rm -f {db}*")
        print(f"=== running {mdl} ===")
        r = run_model(a.bin, db, mdl, a.embed_endpoint, a.gen_endpoint)
        print(json.dumps(r["summary"], indent=2))
        report["models"].append(r)
    json.dump(report, open(a.out, "w"), indent=2)
    print("\n=== COMPARISON ===")
    for r in report["models"]:
        s = r["summary"]
        print(f"{r['model']:26} acc={s['accuracy']} cite={s['citation_rate']} p50={s['p50_latency_s']}s")
    print(f"\nwritten: {a.out}")

if __name__ == "__main__":
    main()
