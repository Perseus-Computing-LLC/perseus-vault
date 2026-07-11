# Retrieval modes

Perseus Vault exposes a broad retrieval surface — keyword, vector, fused, graph,
and proactive/temporal recall — through a single memory store. This page
enumerates every mode in one place: what it is, when to reach for it, how to
invoke it, and a minimal example.

> **Tool names.** The canonical MCP tool prefix is `perseus_vault_*` (the legacy
> `mimir_*` / `mneme_*` aliases remain callable). CLI verbs are shown as
> `perseus-vault <verb>`.

## At a glance

| Mode | Mechanism | Best for | Invocation |
|---|---|---|---|
| **FTS5 (keyword)** | SQLite FTS5, Porter-stemmed **BM25** | Exact terms, identifiers, quotes; fully offline, no embeddings | `perseus_vault_recall` `mode="fts5"` |
| **Dense (vector)** | Bundled 384-dim MiniLM embeddings, cosine similarity | Meaning-based recall, paraphrases, "find things like this" | `perseus_vault_recall` `mode="dense"` |
| **Hybrid (RRF)** | Reciprocal-rank fusion of BM25 + dense | Best general-purpose recall; the default when embeddings exist | `perseus_vault_recall` `mode="hybrid"` (or omit `mode`) |
| **Graph traversal** | Walk the entity link graph to a set depth | "What connects to X"; following decisions/evidence chains | `perseus_vault_traverse` |
| **GraphRAG — communities** | Deterministic community detection over the link graph | Discovering clusters/themes in a large store | `perseus_vault_communities` |
| **GraphRAG — community summary** | Extractive (optionally LLM-polished) summary of one community | A synthesized overview of a cluster, materialized as an entity | `perseus_vault_community_summary` |
| **GraphRAG — global recall** | Breadth over community summaries, then depth into the best | Holistic answers that span clusters, not a single hit | `perseus_vault_global_recall` |
| **Proactive (`recall_when`)** | Surface entities whose `recall_when` triggers match context | Just-in-time injection with no explicit query | `perseus_vault_recall_when` |
| **Temporal (`as_of`)** | Bitemporal point-in-time reconstruction | "What did we believe on date D"; auditing history | `perseus_vault_recall` / `perseus_vault_as_of` with `as_of_unix_ms` |

## Mode selection

`perseus_vault_recall` with **no `mode`** automatically selects **hybrid** whenever
dense embeddings exist for the store, and transparently falls back to **fts5**
keyword search when they do not. Pass `mode` explicitly to pin a specific arm:

- `mode="fts5"` — keyword only; deterministic, zero-embedding, air-gap friendly.
- `mode="dense"` — vector only; meaning-based, no keyword fallback.
- `mode="hybrid"` — both arms fused via RRF (recovers dense's rank-1 dilution
  while keeping keyword precision).

## Examples

Retrieval is invoked as **MCP tool calls** against a running `perseus-vault serve`
(the CLI itself is for admin/write/maintenance). Arguments below are shown as the
`tools/call` payload.

Keyword (BM25):

```jsonc
perseus_vault_recall { "query": "SSRF localhost health check", "mode": "fts5", "limit": 10 }
```

Dense / semantic:

```jsonc
perseus_vault_recall { "query": "how do we keep memory off the GPU", "mode": "dense", "limit": 10 }
```

Hybrid (default when embeddings exist — omit `mode`):

```jsonc
perseus_vault_recall { "query": "recall latency under load", "limit": 10 }
```

GraphRAG global recall (holistic, cross-cluster):

```jsonc
perseus_vault_global_recall { "query": "what are the main architectural themes" }
```

Proactive recall (no query — `recall_when` triggers + context match). Available
as the `perseus_vault_recall_when` tool, and pre-turn via the CLI `prepare` verb:

```bash
perseus-vault prepare --db mimir.db "editing the audit-chain module"
```

Temporal point-in-time (what a fact looked like at an instant):

```jsonc
perseus_vault_recall { "query": "decay policy", "mode": "fts5", "as_of_unix_ms": 1750000000000 }
```

## Notes

- **Query contract: empty string enumerates, `*` is a literal (#562).** An
  **empty** `query` (`""`) is the deliberate **match-all / enumeration** path:
  it drops the keyword predicate and returns every entity in scope (respecting
  `category`, `type`, `limit`, `offset`, and the other filters), ranked by the
  store's default order. Wildcards are **not** globs — `query="*"` is treated as
  the literal FTS5 term `*` and matches nothing (0 results). To "list all", pass
  `query=""`; to search, pass real terms.
- **Paginated enumeration / scan.** `limit` caps a single page and `offset`
  skips rows, so a whole category is walked deterministically by paging with a
  fixed `limit` and advancing `offset` (`0`, `limit`, `2*limit`, …) until a page
  returns fewer than `limit` rows. Use `mode="fts5"` for enumeration — it is the
  deterministic, embedding-free arm, so the order is stable across pages. The
  official Python client wraps this as `VaultClient.scan(category)`, which pages
  under the hood and returns the entire category without a magic cap.
- **Offline by default.** FTS5, dense (bundled model), hybrid, graph, and
  temporal modes all run with **zero network calls**. Dense embeddings can
  optionally be generated via Ollama or an OpenAI-compatible endpoint
  (`perseus_vault_embed`), but the bundled MiniLM model needs neither.
- **Recall quality at scale.** Keyword recall degrades on large distinct-content
  corpora where hybrid holds; see `benchmark/` for measured recall@k across modes.
- **Provenance.** All modes return deterministic, inspectable results; see
  [deterministic-recall-and-provenance.md](deterministic-recall-and-provenance.md).

See also: the full tool reference in the [README](../README.md#mcp-tools).
