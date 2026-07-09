#!/usr/bin/env python3
"""scale_bench.py — WS2: statistically meaningful recall + latency at scale.

Generates a LABELED corpus of topical clusters, embeds it on GPU, then measures
dense vs FTS5 vs hybrid recall@k and latency percentiles at volume. Unlike the
8-entity smoke (rag_bench.py), this produces numbers that actually mean something.

Ground truth: each of C clusters has a canonical concept + M paraphrased entities
(distinct keys, shared cluster_id in a tag). A query paraphrases the concept; a
hit is "relevant" iff the retrieved entity belongs to the query's cluster.
recall@k = (relevant retrieved in top-k) / min(k, cluster_size).

Reuses the MCP JSON-RPC driver from rag_bench.py (same directory).
"""
import argparse, json, random, statistics, time, sys, os

# Distinct filler vocabulary so entities within a cluster are NOT >70% trigram-
# similar (which would trip mimir_remember's content dedup and collapse the
# corpus — see #529 retraction). Each entity gets a unique sentence.
_WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
          "lima mike november oscar papa quebec romeo sierra tango uniform victor "
          "whiskey xray yankee zulu nadir zenith quartz lattice photon vortex ember "
          "cobalt saffron thicket harbor meadow granite plasma tundra cipher fathom "
          "beacon quasar marrow drift ivory basalt cinder").split()

def _filler(seed, n=14):
    r = random.Random(seed)
    return " ".join(r.sample(_WORDS, n))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rag_bench import MCP

# Topical seed vocabulary — each tuple is (concept, [paraphrase fragments], [query paraphrases])
TOPICS = [
    ("vector embeddings for semantic search",
     ["dense vector representations capture meaning for similarity search",
      "embedding models map text into a vector space for nearest-neighbor recall",
      "semantic retrieval uses cosine distance over learned embeddings"],
     ["how do we find semantically similar memories", "meaning-based nearest neighbor lookup"]),
    ("full-text keyword indexing with bm25",
     ["sqlite fts5 provides porter-stemmed keyword search with bm25 ranking",
      "inverted-index keyword matching scores documents by term frequency",
      "lexical search ranks exact and stemmed token overlaps"],
     ["keyword ranking with term frequency", "exact word match search index"]),
    ("air-gapped offline operation",
     ["the vault runs fully offline with zero network calls for classified environments",
      "air-gapped mode disables all remote endpoints and external connectors",
      "IL5 and ICD 503 deployments require no outbound network access"],
     ["run with no internet access classified", "disconnected secure deployment"]),
    ("bi-temporal versioning of facts",
     ["transaction time and valid time let you query what was believed when",
      "facts carry application-time periods separate from when they were recorded",
      "point-in-time reconstruction returns the version live at a past instant"],
     ["what did we believe at a past date", "time travel over fact history"]),
    ("memory decay and consolidation",
     ["ebbinghaus decay scores fade unused memories so recall stays fresh",
      "cold memories are consolidated into durable higher-order insights",
      "sleep-time dreaming merges related episodic memories into semantic ones"],
     ["forgetting old unused memories", "merging related memories into insights"]),
    ("knowledge graph traversal and links",
     ["entities link via depends_on and references to form a traversable graph",
      "graph communities are detected and summarized for global recall",
      "relationship edges let you walk from one memory to related ones"],
     ["walk relationships between memories", "graph of linked entities"]),
    ("encryption at rest",
     ["entity bodies are encrypted with aes-256-gcm keyed per workspace",
      "data at rest is protected with authenticated symmetric encryption",
      "confidential memories stay encrypted in the sqlite store"],
     ["protect stored data with encryption", "aes protection for saved memory"]),
    ("agent self-correction and lessons",
     ["user corrections are captured as durable lessons for self-improvement",
      "the agent records what went wrong and the right approach across sessions",
      "follow-rate signals track whether recalled guidance changed behavior"],
     ["learn from user corrections", "remember mistakes to improve"]),
]

def gen_corpus(clusters, per_cluster):
    """Return list of (category, key, body, cluster_id) and a query list.

    Each cluster gets a UNIQUE synthetic concept (topic template + cluster index
    woven into the text) so ground truth is not diluted across clusters that
    reuse the same base topic. This is what makes recall@k meaningful.
    """
    rows, queries = [], []
    for ci in range(clusters):
        concept, frags, qs = TOPICS[ci % len(TOPICS)]
        # Unique per-cluster token so each cluster is its own retrievable concept.
        marker = f"projectcode{ci:04d}"
        cat = f"cluster{ci:03d}"
        for j in range(per_cluster):
            frag = frags[j % len(frags)]
            body = (f"{frag}. This concerns {marker}: {concept}. "
                    f"Entry {ci}-{j}. {_filler(ci * 1000 + j)}.")
            rows.append((cat, f"{cat}-e{j:03d}", body, ci))
        # Queries reference the UNIQUE marker so exactly one cluster is the gold set.
        for q in qs:
            queries.append((f"{q} regarding {marker}", ci))
    return rows, queries

def recall_at_k(mcp, queries, k, mode, cluster_size):
    """recall@k = fraction of queries whose gold cluster appears in the top-k results.

    Binary hit per query (gold cluster present in top-k or not), averaged over
    queries. Unambiguous and standard: 'did the right memory surface in the top k'.
    """
    hits, lats = [], []
    for q, gold_ci in queries:
        t = time.time()
        r = mcp.tool("mimir_recall", {"query": q, "mode": mode, "limit": k})
        lats.append((time.time()-t)*1000)
        items = r.get("items", []) if isinstance(r, dict) else (r or [])
        gold_cat = f"cluster{gold_ci:03d}"
        hit = any(isinstance(x, dict) and str(x.get("category","")) == gold_cat
                  for x in items[:k])
        hits.append(1.0 if hit else 0.0)
    return statistics.mean(hits), lats

def pct(xs, p):
    xs = sorted(xs); 
    return round(xs[min(len(xs)-1, int(len(xs)*p/100))], 1) if xs else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True); ap.add_argument("--db", required=True)
    ap.add_argument("--llm-endpoint", required=True); ap.add_argument("--llm-model", required=True)
    ap.add_argument("--embedding-endpoint", required=True); ap.add_argument("--embedding-model", required=True)
    ap.add_argument("--clusters", type=int, default=200)
    ap.add_argument("--per-cluster", type=int, default=8)
    ap.add_argument("--tier", default="unknown", help="hardware tier label for the report")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rows, queries = gen_corpus(a.clusters, a.per_cluster)
    total = len(rows)
    print(f"corpus: {total} entities in {a.clusters} clusters x {a.per_cluster}; {len(queries)} queries")

    argv = [a.bin, "serve", "--db", a.db,
            "--llm-endpoint", a.llm_endpoint, "--llm-model", a.llm_model,
            "--embedding-endpoint", a.embedding_endpoint]
    mcp = MCP(argv)
    out = {"tier": a.tier, "corpus": {"entities": total, "clusters": a.clusters,
           "per_cluster": a.per_cluster, "queries": len(queries)}, "summary": {}}
    try:
        # Seed
        t0 = time.time()
        for cat, key, body, _ in rows:
            mcp.tool("mimir_remember", {"category": cat, "key": key,
                     "body_json": json.dumps({"content": body})})
        seed_dt = time.time()-t0
        out["summary"]["seed"] = {"secs": round(seed_dt,2),
                                  "entities_per_sec": round(total/seed_dt,1)}
        print(f"seeded in {seed_dt:.1f}s")

        # Embed on GPU (per cluster category). embed_entity caps each call at
        # batch_limit and only touches entities lacking an embedding, so loop per
        # category until a call reports 0 newly embedded (full coverage).
        t0 = time.time(); n = 0
        cats = sorted({c for c,_,_,_ in rows})
        for c in cats:
            guard = 0
            while guard < 50:
                guard += 1
                e = mcp.tool("mimir_embed", {"batch_category": c, "batch_limit": 5000})
                got = (e.get("embedded", e.get("count", 0)) or 0)
                n += got
                if got == 0:
                    break
        emb_dt = time.time()-t0
        # Verify actual stored coverage; keep ONLY the integer, never the full
        # mimir_stats blob (it carries a per-category map that is huge at scale).
        cov = mcp.tool("mimir_stats", {})
        embedded_count = None
        if isinstance(cov, dict):
            embedded_count = cov.get("embedding_coverage") or cov.get("total_entities")
        out["summary"]["embedding"] = {"entities_embedded": n, "secs": round(emb_dt,2),
                                       "entities_per_sec": round(n/emb_dt,1) if emb_dt else None,
                                       "stored_coverage": embedded_count,
                                       "backend": f"{a.embedding_model} ({a.tier})"}
        print(f"embedded {n} in {emb_dt:.1f}s ({n/emb_dt:.1f}/s), stored_coverage={embedded_count}")

        # Recall@k across modes
        for k in (1, 5, 10):
            for mode in ("fts5", "dense", "hybrid"):
                try:
                    r, lats = recall_at_k(mcp, queries, k, mode, a.per_cluster)
                    out["summary"].setdefault(f"recall@{k}", {})[mode] = {
                        "recall": round(r,3),
                        "p50_ms": pct(lats,50), "p99_ms": pct(lats,99)}
                    print(f"recall@{k} {mode}: {r:.3f}  p50={pct(lats,50)}ms")
                except Exception as e:
                    out["summary"].setdefault(f"recall@{k}", {})[mode] = {"error": str(e)[:200]}
    finally:
        mcp.close()
    json.dump(out, open(a.out,"w"), indent=2)
    print("\n" + json.dumps(out["summary"], indent=2))
    print(f"\nwritten: {a.out}")

if __name__ == "__main__":
    main()
