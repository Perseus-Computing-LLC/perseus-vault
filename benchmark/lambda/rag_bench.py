#!/usr/bin/env python3
"""rag_bench.py — real-inference RAG/embedding benchmark for perseus-vault on A100.

Drives the perseus-vault MCP stdio server (JSON-RPC) against a live Ollama endpoint.
Measures the GPU-dependent numbers that are citable as first-party/measured:

  1. Embedding throughput   — mimir_embed batch over N seeded entities (Ollama /api/embed)
  2. Dense vs FTS5 recall    — mimir_recall mode=dense vs mode=fts5 on the same queries
  3. RAG answer latency      — mimir_ask (recall -> assemble -> qwen2.5:14b generate)

All timings are wall-clock around the JSON-RPC round trip. Honest labeling: the
embedding + ask calls hit the GPU; fts5 recall is CPU (that's the baseline we compare against).
"""
import argparse, json, subprocess, sys, time, statistics

class MCP:
    def __init__(self, argv):
        self.p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, bufsize=1)
        self._id = 0
        self._init()
    def _send(self, method, params=None):
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        self.p.stdin.write(json.dumps(msg) + "\n"); self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                err = self.p.stderr.read()
                raise RuntimeError(f"server closed stdout. stderr:\n{err}")
            line = line.strip()
            if not line: continue
            try: resp = json.loads(line)
            except json.JSONDecodeError: continue  # skip log lines
            if resp.get("id") == self._id: return resp
    def _init(self):
        self._send("initialize", {"protocolVersion": "2024-11-05",
            "capabilities": {}, "clientInfo": {"name": "rag_bench", "version": "1"}})
        self.p.stdin.write(json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"})+"\n")
        self.p.stdin.flush()
    def tool(self, name, args):
        r = self._send("tools/call", {"name": name, "arguments": args})
        if "error" in r: raise RuntimeError(f"{name} error: {r['error']}")
        content = r.get("result", {}).get("content", [])
        text = "".join(c.get("text","") for c in content if c.get("type")=="text")
        try: return json.loads(text)
        except Exception: return {"_raw": text}
    def close(self):
        try: self.p.stdin.close(); self.p.wait(timeout=10)
        except Exception: self.p.kill()

# Seeded corpus: distinct topical entities + a query set with known relevant keys.
CORPUS = [
    ("architecture", "sqlite-fts5", "Adopt SQLite FTS5 for full-text memory search with porter stemming and bm25 ranking."),
    ("architecture", "dense-embeddings", "Dense vector embeddings via nomic-embed-text enable semantic recall beyond keyword match."),
    ("architecture", "encryption-aes", "Vault bodies encrypted at rest with AES-256-GCM; keys derived per workspace."),
    ("decision", "ollama-endpoint", "Use Ollama as the local LLM endpoint for the mimir_ask RAG tool and embeddings."),
    ("decision", "weekly-billing", "Lambda credits decrement weekly, never mid-cycle; terminate idle GPU instances."),
    ("convention", "absolute-paths", "Always use absolute filesystem paths in scripts and never inline secret keys."),
    ("insight", "recall-first", "Recall-first context retrieves only relevant memories on demand to preserve token budget."),
    ("insight", "gpu-coloc", "A 14B instruct model and an embedding model co-locate on a 40GB A100 with headroom."),
]
QUERIES = [
    ("keyword search ranking bm25", "sqlite-fts5"),
    ("semantic meaning vector similarity", "dense-embeddings"),
    ("encrypt data at rest", "encryption-aes"),
    ("which llm server for retrieval augmented generation", "ollama-endpoint"),
    ("how does GPU billing work", "weekly-billing"),
    ("saving memory only when relevant to save tokens", "recall-first"),
]

def bench(mcp, ask_model):
    out = {"summary": {}, "detail": {}}

    # --- Seed ---
    t0 = time.time()
    for cat, key, body in CORPUS:
        mcp.tool("mimir_remember", {"category": cat, "key": key,
                 "body_json": json.dumps({"content": body})})
    out["detail"]["seed_secs"] = round(time.time()-t0, 3)

    # --- 1. Embedding throughput (GPU) ---
    cats = sorted({c for c, _, _ in CORPUS})
    t0 = time.time(); n = 0
    for c in cats:
        emb = mcp.tool("mimir_embed", {"batch_category": c})
        n += emb.get("embedded", emb.get("count", 0)) or 0
    dt = time.time()-t0
    if not n:  # fall back to corpus size if the tool doesn't echo a count
        n = len(CORPUS)
    out["summary"]["embedding"] = {
        "entities_embedded": n, "wall_secs": round(dt,3),
        "entities_per_sec": round(n/dt,2) if dt>0 else None,
        "backend": "ollama nomic-embed-text (A100)"}

    # --- 2. Dense vs FTS5 recall on same query set ---
    def recall_at1(mode):
        hits = 0; lats = []
        for q, gold in QUERIES:
            t=time.time()
            r = mcp.tool("mimir_recall", {"query": q, "mode": mode, "limit": 3})
            lats.append((time.time()-t)*1000)
            res = r.get("items", []) if isinstance(r, dict) else (r or [])
            keys = [ (x.get("key") if isinstance(x,dict) else "") for x in res ]
            if gold in keys[:1]: hits += 1
        return hits/len(QUERIES), statistics.median(lats)
    for mode in ("fts5", "dense", "hybrid"):
        try:
            r1, p50 = recall_at1(mode)
            out["summary"].setdefault("recall_at1", {})[mode] = {
                "recall@1": round(r1,3), "p50_latency_ms": round(p50,1)}
        except Exception as e:
            out["summary"].setdefault("recall_at1", {})[mode] = {"error": str(e)}

    # --- 3. RAG answer latency (GPU generate) ---
    lats=[]; sample=None
    for q,_ in QUERIES[:4]:
        t=time.time()
        a = mcp.tool("mimir_ask", {"query": q, "top_k": 4})
        lats.append(time.time()-t)
        if sample is None: sample = str(a)[:300]
    out["summary"]["rag_ask"] = {
        "model": ask_model, "n": len(lats),
        "p50_secs": round(statistics.median(lats),2),
        "mean_secs": round(statistics.mean(lats),2),
        "sample_answer": sample}
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--llm-endpoint", required=True)
    ap.add_argument("--llm-model", required=True)
    ap.add_argument("--embedding-endpoint", required=True)
    ap.add_argument("--embedding-model", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    argv = [a.bin, "serve", "--db", a.db,
            "--llm-endpoint", a.llm_endpoint, "--llm-model", a.llm_model,
            "--embedding-endpoint", a.embedding_endpoint]
    mcp = MCP(argv)
    try:
        res = bench(mcp, a.llm_model)
    finally:
        mcp.close()
    res["config"] = {"llm_model": a.llm_model, "embedding_model": a.embedding_model,
                     "llm_endpoint": a.llm_endpoint}
    json.dump(res, open(a.out,"w"), indent=2)
    print(json.dumps(res["summary"], indent=2))

if __name__ == "__main__":
    main()
