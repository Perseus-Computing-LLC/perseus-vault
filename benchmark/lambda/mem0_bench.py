#!/usr/bin/env python3
"""mem0_bench.py — WS3 competitive: run the SAME memory-recall task against Mem0
that competitive_bench.py runs against Perseus Vault, on the SAME box + local Ollama.

Honest-labeling: this produces a MEASURED Mem0 number (source=measured) only if Mem0
installs and runs locally here. If install/config fails, it records the failure
verbatim (source=install_failed) rather than substituting a published number as if
measured. Findings comparing weaknesses stay in the PRIVATE competitive-intelligence
skill; this script only emits neutral measured metrics.

Task parity with Perseus Vault (quality_lift.py FACTS/QUESTIONS): seed the same facts,
ask the same questions, judge by expected-substring, record accuracy + latency.
"""
import argparse, json, time, sys, statistics

FACTS = [
    "Perseus Vault stores memories in SQLite with FTS5 and optional dense embeddings via Ollama.",
    "The vault runs fully air-gapped: offline mode disables all network calls for IL5/ICD-503 use.",
    "Bi-temporal versioning tracks both transaction time (when recorded) and valid time (when true).",
    "Ebbinghaus decay fades unused memories; consolidation merges cold ones into durable insights.",
    "Hybrid recall fuses dense vector search with FTS5 keyword search via reciprocal rank fusion.",
    "Entities are encrypted at rest with AES-256-GCM, keyed per workspace.",
]
QUESTIONS = [
    ("Can the system work without any internet connection, and how?", ["offline", "air-gap"]),
    ("How does it distinguish what was believed versus what was true at a past time?", ["transaction", "valid"]),
    ("What happens to memories that are never accessed?", ["decay", "fade", "consolidat"]),
    ("How does it combine keyword and semantic search?", ["hybrid", "fusion", "rank"]),
    ("Is stored data protected if the database file is stolen?", ["encrypt", "aes"]),
]

def judge(ans, keys):
    a = (ans or "").lower()
    return any(k.lower() in a for k in keys)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ollama-base", default="http://localhost:11434")
    ap.add_argument("--chat-model", default="qwen2.5:14b-instruct")
    ap.add_argument("--embed-model", default="nomic-embed-text")
    ap.add_argument("--tier", default="2xH100-80GB")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    report = {"system": "mem0", "tier": a.tier, "task": "LOCOMO-style memory recall",
              "chat_model": a.chat_model, "source": "measured"}
    try:
        from mem0 import Memory
    except Exception as e:
        report["source"] = "install_failed"
        report["error"] = f"import mem0 failed: {e}"
        json.dump(report, open(a.out, "w"), indent=2)
        print(json.dumps(report, indent=2)); return

    # Configure Mem0 fully local: Ollama LLM + Ollama embedder + local vector store.
    config = {
        "llm": {"provider": "ollama",
                "config": {"model": a.chat_model, "ollama_base_url": a.ollama_base}},
        "embedder": {"provider": "ollama",
                     "config": {"model": a.embed_model, "ollama_base_url": a.ollama_base,
                                "embedding_dims": 768}},
        "vector_store": {"provider": "qdrant",
                         "config": {"path": "/tmp/mem0_qdrant", "on_disk": True,
                                    "embedding_model_dims": 768}},
    }
    try:
        mem = Memory.from_config(config)
    except Exception as e:
        report["source"] = "config_failed"
        report["error"] = f"Memory.from_config failed: {e}"
        json.dump(report, open(a.out, "w"), indent=2)
        print(json.dumps(report, indent=2)); return

    t0 = time.time()
    for i, f in enumerate(FACTS):
        mem.add(f, user_id="bench")
    seed_dt = time.time() - t0

    probes = []
    for q, keys in QUESTIONS:
        t = time.time()
        try:
            res = mem.search(q, user_id="bench", limit=5)
            # Mem0 returns dict with 'results' or a list depending on version
            hits = res.get("results", res) if isinstance(res, dict) else res
            joined = " ".join(str(h.get("memory", h)) if isinstance(h, dict) else str(h)
                              for h in (hits or []))
        except Exception as e:
            joined = f"search_error: {e}"
        probes.append({"q": q, "correct": judge(joined, keys),
                       "latency_s": round(time.time() - t, 2),
                       "retrieved": joined[:200]})
    n = len(probes)
    report["summary"] = {
        "seed_secs": round(seed_dt, 2),
        "recall_accuracy": round(sum(p["correct"] for p in probes) / n, 3),
        "mean_search_latency_s": round(statistics.mean(p["latency_s"] for p in probes), 2),
    }
    report["probes"] = probes
    json.dump(report, open(a.out, "w"), indent=2)
    print(json.dumps(report["summary"], indent=2))

if __name__ == "__main__":
    main()
