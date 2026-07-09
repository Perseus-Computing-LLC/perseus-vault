#!/usr/bin/env python3
"""competitive_bench.py — WS3: Perseus Vault vs Mem0 / Zep / Letta, same corpus + hardware.

Honest-labeling policy (per team convention): every number is tagged with its
source — "measured" (we ran it here) or "published" (cited, with URL). We never
present a published competitor number as if we measured it. Findings that touch
competitor weaknesses go to the PRIVATE competitive-intelligence skill, NOT public issues.

This driver runs the MEASURED side for Perseus Vault (via the MCP server) on a
standard memory task, and records competitor numbers as either local runs (when
the competitor installs cleanly with a local LLM backend) or published citations.

Task: LOCOMO-style long-conversation memory recall — seed N facts, then ask
questions whose answers require retrieving the right fact. Metrics: answer
correctness (LLM-judged exact/contains), retrieval latency, tokens to context.
"""
import argparse, json, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rag_bench import MCP

# Minimal LOCOMO-style probe set: (fact to store, question, expected substring)
PROBES = [
    ("The user's project is called Perseus Vault, a local-first agentic memory engine.",
     "What is the user's project called?", "Perseus Vault"),
    ("The user deploys on air-gapped networks for defense customers at IL5.",
     "What security level does the user deploy at?", "IL5"),
    ("The user prefers Ollama with qwen2.5 for local inference.",
     "Which local inference stack does the user prefer?", "Ollama"),
    ("The vault stores memories in SQLite with FTS5 and optional dense embeddings.",
     "What database does the vault use?", "SQLite"),
    ("The user's embedding model of choice is nomic-embed-text.",
     "Which embedding model does the user use?", "nomic-embed-text"),
]

def judge(answer, expected):
    return expected.lower() in (answer or "").lower()

def run_perseus(argv_serve):
    mcp = MCP(argv_serve)
    res = {"system": "perseus-vault", "source": "measured", "probes": []}
    try:
        for fact, q, exp in PROBES:
            mcp.tool("mimir_remember", {"category": "conv", "key": f"f{len(res['probes'])}",
                     "body_json": json.dumps({"content": fact})})
        for cat in ["conv"]:
            mcp.tool("mimir_embed", {"batch_category": cat})
        for fact, q, exp in PROBES:
            t = time.time()
            a = mcp.tool("mimir_ask", {"query": q, "top_k": 5})
            dt = time.time()-t
            ans = a.get("answer", str(a)) if isinstance(a, dict) else str(a)
            res["probes"].append({"q": q, "correct": judge(ans, exp),
                                  "latency_s": round(dt,2), "answer": ans[:200]})
    finally:
        mcp.close()
    n = len(res["probes"]); c = sum(p["correct"] for p in res["probes"])
    res["summary"] = {"accuracy": round(c/n,3) if n else 0,
                      "mean_latency_s": round(sum(p["latency_s"] for p in res["probes"])/n,2) if n else None}
    return res

# Competitor slots. When a competitor installs cleanly with a LOCAL llm backend,
# implement run_<name>() the same way and set source="measured". Until then these
# carry PUBLISHED numbers with citations — clearly labeled, never fabricated.
PUBLISHED = {
    "mem0":  {"source": "published", "note": "fill from mem0 paper/docs with URL before use"},
    "zep":   {"source": "published", "note": "fill from zep benchmarks with URL before use"},
    "letta": {"source": "published", "note": "fill from letta/MemGPT paper with URL before use"},
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True); ap.add_argument("--db", required=True)
    ap.add_argument("--llm-endpoint", required=True); ap.add_argument("--llm-model", required=True)
    ap.add_argument("--embedding-endpoint", required=True)
    ap.add_argument("--tier", default="unknown"); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    argv = [a.bin, "serve", "--db", a.db, "--llm-endpoint", a.llm_endpoint,
            "--llm-model", a.llm_model, "--embedding-endpoint", a.embedding_endpoint]
    report = {"tier": a.tier, "task": "LOCOMO-style memory recall", "systems": []}
    report["systems"].append(run_perseus(argv))
    for name, meta in PUBLISHED.items():
        report["systems"].append({"system": name, **meta})
    json.dump(report, open(a.out,"w"), indent=2)
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
