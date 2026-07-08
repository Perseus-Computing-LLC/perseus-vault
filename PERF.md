# PERF.md — measured optimizations (before/after)

Every entry: fixed hardware, named binary/commit, the profile that led to the
change, and the numbers. Companion to `benchmark/scale/` (signed baselines +
CI budgets) and the #473 epic's rule: no claim without a rerunnable script.

## #507 — dense recall: covering index for the phase-0 signature scan (v18)

**Hardware:** same box as #476. A/B on the IDENTICAL loaded 100K store (kept
from the scale harness), 50 queries/mode, before = the #476-merged binary,
after = this change; the after binary's first open runs the v18 migration.

### Where the time went

`dense_search` phase 0 — the "cheap" sign-bit prefilter — and the embedded-row
count both predicate on `embedding IS NOT NULL`. `embedding`/`emb_sig` are
late ALTER columns stored AFTER `body_json` in each record, so evaluating the
predicate (and reading `emb_sig`) walked every row's multi-KB body overflow
chain: ~900MB of page reads per dense query at 100K. `embedding_coverage()`,
consulted per recall to pick the default mode, paid the same walk.

### The fix (v18)

`idx_entities_dense_sig ON entities(archived, id, emb_sig) WHERE emb_sig IS
NOT NULL` — every column the phase-0 queries touch is an index column, so
they plan as USING COVERING INDEX (~60B/row; zero record reads), pinned by a
plan-text regression test. The queries re-key on `emb_sig IS NOT NULL`, made
exact by the v18 invariant "embedded ⟺ signed": the migration backfills
`emb_sig` from every stored embedding (pure sign-bit recompute, no model),
and writers already set/clear both columns together. Two variants that do
NOT work, for the record: the `embedding IS NOT NULL` spelling never covers
(residual predicate seeks the table), and an expression index only covers on
SQLite ≥ 3.5x — newer than the bundled engine.

### Numbers (100K entities, identical store)

| Mode | Before p50/p99 | After p50/p99 | Δ p50 |
| --- | --- | --- | --- |
| dense | 360.3 / 390.9 ms | **24.9 / 29.3 ms** | **14.5×** |
| hybrid | 593.9 / 620.7 ms | 308.4 / 325.3 ms | 1.9× |
| fts5 | 14.6 / 17.9 ms | 17.0 / 19.4 ms | unchanged (noise) |

### Residual (next target)

Hybrid ≫ dense + fts5 (308 vs ~42 ms): roughly 265ms lives in the hybrid-only
machinery (RRF fusion candidate over-fetch and hydration, query expansion,
graph expand's per-candidate link following) — filed as its own issue with
this A/B as the baseline.

## #476 — write path: signature-driven near-duplicate scan (v17)

**Hardware:** AMD64 16-core (AMD Family 26), Windows 11 · `benchmark/scale/run.py`,
MCP stdio, one persistent process, seeded corpus, ~40–120-word bodies (~9KB stored).

### Where the time went

The per-write near-duplicate scan walked **every same-category entity row**:
each candidate's multi-KB `body_json` was hydrated (overflow pages included)
and re-hashed with `body_hash64` as the signature freshness guard — even
though the #392 signature machinery could already decide verdicts without the
body. Cost per write: O(N·body_size) — ~90MB read+hashed per insert at 10K
rows, ~900MB at 100K. Attribution was confirmed empirically before the fix:
removing the embedding stack entirely (lite build) still showed the 15×
first-to-last-10% degradation, and the opt-in FTS prefilter (#228) measured
*slower* than the scan it pruned (a 64-term OR MATCH per write).

### The fix (v17, exactness-preserving)

1. `dedup_signatures` gains scope columns + a `(category, workspace_hash,
   tg_count)` index; a one-time migration backfills every active row's
   signature ("every active row has a signature" becomes an invariant).
2. The scan walks **signatures only** — small fixed-size rows, SQL-band-pruned
   by the lossless trigram-count ratio bound (`J ≥ t ⟹ min(a,b)/max(a,b) ≥ t`),
   then the existing lossless count/histogram prunes + exact merge verdict.
3. Freshness moves to **verify-on-hit**: only a candidate whose signature says
   "dup" gets its body fetched and re-checked (hash + scope + archived), with
   self-healing repair. Never a false positive; the deliberate trade is that a
   row rewritten behind the engine's back can be missed (one extra stored row)
   until it self-heals — the old guard taxed every write for everyone to cover
   that rare case. The lossy FTS prefilter is retired outright.

### Numbers

| Metric | Before (2.19.0) | After (this change) | Δ |
| --- | --- | --- | --- |
| 10K load, sustained | 141/s | **554/s** | **3.9×** |
| 10K load, first→last 10% | 1107 → 68/s (16×) | 1197 → 349/s (3.4×) | 5.1× at the tail |
| 100K load, sustained | 7/s (~4.0h wall) | **39/s (43min wall)** | **5.6×** |
| 100K load, first→last 10% | 117 → 3/s | 483 → 18/s | 6× at the tail |
| 100K fts5 recall p50/p99 | 16.5 / 181.7 ms | 16.1 / **21.7** ms | p99 spikes gone¹ |
| 100K `as_of` p99 | 0.32 ms | 0.26 ms | unchanged path |
| 100K cold start | 70.2 ms | 71.7 ms | unchanged path |

¹ The baseline's fts5 p99 outliers were dedup I/O pressure from the write
phase's page-cache churn; with bodies out of the scan they disappear.

Measurement note: the AFTER runs shared the machine with an API-paced
LongMemEval harness (bursty local ingest); the BEFORE baselines ran clean.
The improvement figures are therefore lower bounds.

Verdict-correctness is pinned by the randomized differential property test
(`find_near_duplicate_signature_path_matches_exhaustive_scan_property`)
against the verbatim pre-#392 exhaustive reference, plus contract tests for
the verify-on-hit guard (no false positives, self-heal) and raw-row
visibility. Full suite: 383 passed.

### Residual (next targets)

The remaining 10K tail decay (1197 → 349/s) is embed-on-write CPU (the lite
build measured a flat ~35% embed tax) and FTS5/WAL growth — see #507 (dense
recall brute-force scan, the read-side sibling) and the scale-gate budgets
that lock today's numbers in.
