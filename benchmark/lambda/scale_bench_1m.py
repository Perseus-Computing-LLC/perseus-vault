#!/usr/bin/env python3
"""scale_bench_1m.py — WS2 at 1,000,000 entities (fleet-embedded).

Extends scale_bench.py (the 100k harness) to the 1M scale point. Three minimal,
documented deltas over scale_bench.py; everything else (corpus generator, recall
definition, MCP driver) is reused verbatim so the 1M number is comparable to 100k:

  (a) FLEET EMBEDDING (--embed-fleet N).  The binary's mimir_embed batch loop is
      strictly serial (one blocking /api/embed per entity, then store) so it caps
      at ~15 emb/s regardless of GPU count — 1M would take ~18h. Instead we embed
      client-side: read every un-embedded (id, body_json) straight from the SQLite
      DB, fan the /api/embed calls out concurrently round-robin across the N pinned
      per-GPU Ollama daemons (ports 11434..11434+N-1, brought up by serve_fleet.sh),
      and write the result back into the DB in the EXACT on-disk format the binary
      uses:  embedding = little-endian f32 bytes (db.rs store_embedding_with_conn),
      emb_sig = sign-bit signature, ceil(dim/8) bytes, bit i set iff v[i] > 0.0
      (db.rs embedding_signature). Both columns are written together to preserve the
      v18 "embedded ⟺ signed" invariant the dense-recall signature prefilter relies
      on. The text embedded is body_json read verbatim from the row — byte-identical
      to what the binary's own batch loop embeds — so the vectors match the serial
      path exactly, just produced ~40x faster. The MCP/serve process must be closed
      while this runs (single-writer SQLite); the orchestrator does that.

  (b) QUERY SAMPLING (--sample-queries N / --sample-seed S). 10000 clusters x 2
      paraphrases = 20000 queries; measuring recall over all of them x 3 modes is
      slow and unnecessary. Sample N (fixed seed) for a tractable, reproducible
      recall estimate.

  (c) WARM-SET (--warm-set). dense_search (db.rs) is brute-force with a hard
      max_scan = 50_000 ceiling and no HNSW: at 1M it ranks candidates over only the
      50k rows with the smallest id (idx_entities_dense_sig is on (archived,id,...)).
      Entity ids are random (mem-<uuid>), so that 50k is a uniform ~5% sample across
      ALL clusters, NOT the first 500 clusters. We therefore report TWO recall
      columns: "uniform" (all clusters — the true stock-binary number, some clusters
      may have zero of their entities in the capped scan) and "warm_set" (only
      queries whose gold cluster is actually represented in the scanned 50k — i.e.
      reachable under the cap). The gap between them is the measurable cost of the
      50k ceiling at 1M. Neither is rigged; the warm-set membership is read back from
      the live DB after embedding.

  Recall is also computed with ONE recall(limit=max_k) call per (query,mode),
  deriving @1/@5/@10 from the single ranked list, instead of one call per k — 3x
  fewer round trips, identical results.
"""
import argparse, json, random, statistics, time, sys, os, sqlite3, struct, threading, queue, urllib.request

# ---- reused verbatim from scale_bench.py: distinct filler + topical corpus ----
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
    rows, queries = [], []
    for ci in range(clusters):
        concept, frags, qs = TOPICS[ci % len(TOPICS)]
        marker = f"projectcode{ci:04d}"
        cat = f"cluster{ci:03d}"
        for j in range(per_cluster):
            frag = frags[j % len(frags)]
            body = (f"{frag}. This concerns {marker}: {concept}. "
                    f"Entry {ci}-{j}. {_filler(ci * 1000 + j)}.")
            rows.append((cat, f"{cat}-e{j:03d}", body, ci))
        for q in qs:
            queries.append((f"{q} regarding {marker}", ci))
    return rows, queries

# ---- (a) fleet embedding: client-side, direct DB write, binary-identical format ----
def _emb_sig(vec):
    """Replicate db.rs embedding_signature exactly: ceil(dim/8) bytes, bit i set iff v[i]>0."""
    n = (len(vec) + 7) // 8
    sig = bytearray(n)
    for i, x in enumerate(vec):
        if x > 0.0:
            sig[i // 8] |= (1 << (i % 8))
    return bytes(sig)

def _embed_one(port, text, model, retries=3):
    body = json.dumps({"model": model, "input": text}).encode()
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/embed", data=body,
                                         headers={"Content-Type": "application/json"})
            r = json.loads(urllib.request.urlopen(req, timeout=120).read())
            emb = r.get("embeddings", [None])[0]
            if emb and len(emb) > 100:
                return emb
            last = f"bad dim {len(emb) if emb else None}"
        except Exception as e:
            last = str(e)[:120]
            time.sleep(0.5)
    raise RuntimeError(f"embed failed on :{port}: {last}")

def fleet_embed(db_path, nports, base_port, model, concurrency):
    """Embed every un-embedded, non-archived entity across N per-GPU daemons and
    write embedding+emb_sig back to the DB. Returns (embedded, secs, dim)."""
    rconn = sqlite3.connect(db_path, timeout=120)
    todo = rconn.execute(
        "SELECT id, body_json FROM entities WHERE embedding IS NULL AND archived = 0"
    ).fetchall()
    rconn.close()
    total = len(todo)
    print(f"fleet-embed: {total} entities to embed across {nports} daemons "
          f"(ports {base_port}..{base_port+nports-1}), conc={concurrency}", flush=True)
    if total == 0:
        return 0, 0.0, None

    from concurrent.futures import ThreadPoolExecutor
    ports = [base_port + i for i in range(nports)]
    results = queue.Queue(maxsize=20000)
    dim_holder = {}
    counters = {"done": 0, "err": 0}
    t0 = time.time()

    def worker(args):
        idx, ent_id, body = args
        try:
            emb = _embed_one(ports[idx % nports], body, model)
            if "d" not in dim_holder:
                dim_holder["d"] = len(emb)
            blob = struct.pack(f"<{len(emb)}f", *emb)
            sig = _emb_sig(emb)
            results.put((ent_id, blob, sig))
        except Exception as e:
            counters["err"] += 1
            if counters["err"] <= 20:
                print(f"  embed error: {e}", flush=True)

    def writer():
        wconn = sqlite3.connect(db_path, timeout=120)
        wconn.execute("PRAGMA busy_timeout=120000")
        batch = []
        while True:
            item = results.get()
            if item is None:
                break
            batch.append(item)
            if len(batch) >= 2000:
                wconn.executemany(
                    "UPDATE entities SET embedding=?1, emb_sig=?2 WHERE id=?3",
                    [(b, s, i) for (i, b, s) in batch])
                wconn.commit()
                counters["done"] += len(batch)
                batch = []
                if counters["done"] % 50000 < 2000:
                    rate = counters["done"] / max(1e-6, time.time() - t0)
                    print(f"  embedded {counters['done']}/{total} "
                          f"({rate:.0f}/s, {counters['err']} err)", flush=True)
        if batch:
            wconn.executemany(
                "UPDATE entities SET embedding=?1, emb_sig=?2 WHERE id=?3",
                [(b, s, i) for (i, b, s) in batch])
            wconn.commit()
            counters["done"] += len(batch)
        wconn.close()

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(worker, ((i, r[0], r[1]) for i, r in enumerate(todo))))
    results.put(None)
    wt.join()
    dt = time.time() - t0
    dim = dim_holder.get("d")
    print(f"fleet-embed done: {counters['done']} embedded, {counters['err']} errors, "
          f"{dt:.1f}s ({counters['done']/dt:.1f}/s), dim={dim}", flush=True)
    return counters["done"], round(dt, 2), dim

# ---- (c) which clusters are represented in the capped 50k dense scan ----
def scanned_clusters(db_path, max_scan):
    """Return the set of cluster categories present in the first `max_scan` rows the
    binary's dense_search would scan: idx_entities_dense_sig order = (archived, id).
    This is read back from the live DB — not assumed."""
    conn = sqlite3.connect(db_path, timeout=120)
    cats = conn.execute(
        "SELECT DISTINCT category FROM ("
        "  SELECT category FROM entities WHERE archived = 0 AND emb_sig IS NOT NULL "
        "  ORDER BY archived, id LIMIT ?1)",
        (max_scan,)).fetchall()
    conn.close()
    return {c[0] for c in cats}

# ---- recall: one recall(limit=max_k) per (query,mode); derive @1/@5/@10 ----
KS = (1, 5, 10)
def recall_over(mcp, queries, mode):
    max_k = max(KS)
    hit = {k: [] for k in KS}
    lats = []
    for q, gold_ci in queries:
        gold_cat = f"cluster{gold_ci:03d}"
        t = time.time()
        r = mcp.tool("mimir_recall", {"query": q, "mode": mode, "limit": max_k})
        lats.append((time.time() - t) * 1000)
        items = r.get("items", []) if isinstance(r, dict) else (r or [])
        cats = [str(x.get("category", "")) for x in items if isinstance(x, dict)]
        for k in KS:
            hit[k].append(1.0 if gold_cat in cats[:k] else 0.0)
    return {k: round(statistics.mean(hit[k]), 3) for k in KS}, lats

def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * p / 100))], 1) if xs else None

def measure(mcp, queries, label, out):
    print(f"\n[recall:{label}] n={len(queries)} queries", flush=True)
    for mode in ("fts5", "dense", "hybrid"):
        try:
            rk, lats = recall_over(mcp, queries, mode)
            for k in KS:
                out.setdefault(f"recall@{k}", {}).setdefault(label, {})[mode] = {
                    "recall": rk[k], "p50_ms": pct(lats, 50), "p99_ms": pct(lats, 99)}
            print(f"  {mode}: @1={rk[1]} @5={rk[5]} @10={rk[10]}  p50={pct(lats,50)}ms", flush=True)
        except Exception as e:
            for k in KS:
                out.setdefault(f"recall@{k}", {}).setdefault(label, {})[mode] = {"error": str(e)[:200]}
            print(f"  {mode}: ERROR {e}", flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True); ap.add_argument("--db", required=True)
    ap.add_argument("--llm-endpoint", required=True); ap.add_argument("--llm-model", required=True)
    ap.add_argument("--embedding-endpoint", required=True); ap.add_argument("--embedding-model", required=True)
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--clusters", type=int, default=10000)
    ap.add_argument("--per-cluster", type=int, default=100)
    ap.add_argument("--tier", default="unknown")
    ap.add_argument("--out", required=True)
    # 1M additions
    ap.add_argument("--embed-fleet", type=int, default=0, help="N pinned GPU daemons for client-side embedding (0=use binary serial path)")
    ap.add_argument("--fleet-base-port", type=int, default=11434)
    ap.add_argument("--fleet-concurrency", type=int, default=64)
    ap.add_argument("--sample-queries", type=int, default=2000)
    ap.add_argument("--sample-seed", type=int, default=1337)
    ap.add_argument("--warm-set", action="store_true")
    ap.add_argument("--max-scan", type=int, default=50000, help="binary dense_search max_scan (for warm-set membership)")
    a = ap.parse_args()

    rows, queries = gen_corpus(a.clusters, a.per_cluster)
    total = len(rows)
    print(f"corpus: {total} entities in {a.clusters} clusters x {a.per_cluster}; {len(queries)} queries", flush=True)

    argv = [a.bin, "serve", "--db", a.db,
            "--llm-endpoint", a.llm_endpoint, "--llm-model", a.llm_model,
            "--embedding-endpoint", a.embedding_endpoint,
            "--embedding-model-name", a.embedding_model]

    out = {"tier": a.tier, "corpus": {"entities_generated": total, "clusters": a.clusters,
           "per_cluster": a.per_cluster, "queries_generated": len(queries)}, "summary": {}}

    # ---- Phase 1: seed (via MCP remember — exercises real dedup + workspace) ----
    mcp = MCP(argv)
    try:
        if a.skip_seed:
            print("skip-seed: reusing entities already in DB", flush=True)
            out["summary"]["seed"] = {"skipped": True}
        else:
            t0 = time.time()
            for n, (cat, key, body, _) in enumerate(rows):
                mcp.tool("mimir_remember", {"category": cat, "key": key,
                         "body_json": json.dumps({"content": body})})
                if n and n % 100000 == 0:
                    print(f"  seeded {n}/{total} ({n/(time.time()-t0):.0f}/s)", flush=True)
            seed_dt = time.time() - t0
            out["summary"]["seed"] = {"secs": round(seed_dt, 2),
                                      "entities_per_sec": round(total / seed_dt, 1)}
            print(f"seeded in {seed_dt:.1f}s ({total/seed_dt:.0f}/s)", flush=True)
        # persisted distinct count (dedup gap check) + baseline embedded coverage
        st = mcp.tool("mimir_stats", {})
        persisted = st.get("total_entities") if isinstance(st, dict) else None
        out["corpus"]["persisted"] = persisted
        print(f"persisted entities (post-dedup): {persisted}", flush=True)
    finally:
        mcp.close()  # release the DB before direct-write embedding (single-writer sqlite)

    # ---- Phase 2: embedding ----
    if a.embed_fleet and a.embed_fleet > 0:
        n, dt, dim = fleet_embed(a.db, a.embed_fleet, a.fleet_base_port, a.embedding_model, a.fleet_concurrency)
        out["summary"]["embedding"] = {"mode": "fleet", "n_daemons": a.embed_fleet,
            "entities_embedded": n, "secs": dt, "entities_per_sec": round(n/dt,1) if dt else None,
            "dim": dim, "backend": f"{a.embedding_model} ({a.tier})"}
    else:
        # fallback: binary serial embed (100k-style) — reopen MCP
        mcp = MCP(argv); t0 = time.time(); n = 0
        try:
            for c in sorted({c for c, _, _, _ in rows}):
                guard = 0
                while guard < 60:
                    guard += 1
                    e = mcp.tool("mimir_embed", {"batch_category": c, "batch_limit": 5000})
                    got = (e.get("embedded", e.get("count", 0)) or 0); n += got
                    if got == 0:
                        break
        finally:
            mcp.close()
        dt = time.time() - t0
        out["summary"]["embedding"] = {"mode": "binary-serial", "entities_embedded": n,
            "secs": round(dt,2), "entities_per_sec": round(n/dt,1) if dt else None}

    # verify stored coverage via a direct DB count (authoritative)
    cc = sqlite3.connect(a.db, timeout=120)
    coverage = cc.execute("SELECT COUNT(*) FROM entities WHERE emb_sig IS NOT NULL AND archived=0").fetchone()[0]
    cc.close()
    out["summary"]["embedding"]["stored_coverage"] = coverage
    print(f"stored embedding coverage: {coverage}", flush=True)

    # ---- Phase 3: recall (uniform + warm-set) ----
    rnd = random.Random(a.sample_seed)
    uniform = rnd.sample(queries, min(a.sample_queries, len(queries)))

    warm = None
    if a.warm_set:
        cats = scanned_clusters(a.db, a.max_scan)
        out["summary"]["warm_set_info"] = {"max_scan": a.max_scan,
            "clusters_in_scan": len(cats), "clusters_total": a.clusters}
        reachable = [q for q in queries if f"cluster{q[1]:03d}" in cats]
        rnd2 = random.Random(a.sample_seed)
        warm = rnd2.sample(reachable, min(a.sample_queries, len(reachable))) if reachable else []
        print(f"warm-set: {len(cats)}/{a.clusters} clusters represented in {a.max_scan} scan; "
              f"{len(reachable)} reachable queries", flush=True)

    mcp = MCP(argv)
    try:
        measure(mcp, uniform, "uniform", out["summary"])
        if warm:
            measure(mcp, warm, "warm_set", out["summary"])
    finally:
        mcp.close()

    json.dump(out, open(a.out, "w"), indent=2)
    print("\n" + json.dumps(out["summary"], indent=2), flush=True)
    print(f"\nwritten: {a.out}", flush=True)

if __name__ == "__main__":
    main()
