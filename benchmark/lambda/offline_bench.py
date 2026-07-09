import sys, json, sqlite3, subprocess, time, os
sys.path.insert(0, "/home/ubuntu/lambda-kit")
from rag_bench import MCP
BIN = "/lambda/nfs/perseus-vault-fs-south/repo/perseus-vault/target/release/perseus-vault"
DB = "/tmp/offline.db"
os.system("rm -f %s*" % DB)

# TIER 0: fully air-gapped. --offline disables ALL network (LLM, embed, web, connectors).
# NO --llm-endpoint. Proves core memory + keyword recall work with zero network calls.
argv = [BIN, "serve", "--db", DB, "--offline"]
m = MCP(argv)

# distinct facts (dedup-safe)
FACTS = [
    ("secops", "airgap", "Classified deployments run fully disconnected from any external network."),
    ("secops", "crypto", "Sensitive records are sealed with authenticated symmetric encryption at rest."),
    ("arch", "sqlite", "The store is a single embedded SQLite database file with full text search."),
    ("arch", "temporal", "Historical versions are reconstructable at any past instant on demand."),
    ("ops", "decay", "Rarely touched notes gradually lose ranking weight over elapsed time."),
]
created = 0
for cat, key, body in FACTS:
    r = m.tool("mimir_remember", {"category": cat, "key": key, "body_json": json.dumps({"content": body})})
    if isinstance(r, dict) and r.get("action") == "created":
        created += 1

QUERIES = [
    ("disconnected network classified", "airgap"),
    ("encryption at rest sensitive", "crypto"),
    ("full text search sqlite", "sqlite"),
    ("reconstruct past version history", "temporal"),
]
hits = 0
lat = []
for q, gold in QUERIES:
    t = time.time()
    r = m.tool("mimir_recall", {"query": q, "mode": "fts5", "limit": 3})
    lat.append((time.time()-t)*1000)
    items = r.get("items", []) if isinstance(r, dict) else r
    keys = [it.get("key") for it in items[:3]]
    if gold in keys:
        hits += 1
m.close()
time.sleep(1)
persisted = sqlite3.connect(DB).execute("SELECT count(*) FROM entities").fetchone()[0]
print(json.dumps({
    "tier": "0-offline-airgapped",
    "mode": "--offline (zero network, no LLM/embed endpoint)",
    "created": created, "persisted": persisted,
    "fts5_recall@3": round(hits/len(QUERIES), 3),
    "fts5_p50_ms": round(sorted(lat)[len(lat)//2], 2),
}, indent=2))
