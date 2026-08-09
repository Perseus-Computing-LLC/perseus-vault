# Optional quantized embedding storage (vector compression)

Status: implementation specification
Date: 2026-08-09
Resolves: #885
Related: `evidence-grounded-observations.md` (#884), `validity-aware-recall.md`
(#860), the #619 dense-arm design (signature prefilter, int4 refine tiers),
insight `mnemosyne-hindsight-differentiation-2026-08-07` (competitive scan:
Mnemosyne MIB binarization — 384-dim float32 → 48-byte binary vectors, Hamming
scoring, sub-linear storage growth, flat recall from 100K to 10M).

## Motivation

The Vault host is RAM-constrained (~4GB) and embedding storage scales linearly
with store size: a 384-dim float32 vector costs 1536 bytes stored. Mnemosyne's
MIB-style binarization stores the sign bit of every dimension (48 bytes for
384-dim — 1/32 of float32) and scores by Hamming distance over the stored bits.
This spec adds the same opt-in capability: int8 and bit storage modes for the
`entities.embedding` column, in-store distance scoring (Hamming for bit),
a config flag, and a documented reindex/migration path from float32 with a
lossless rollback. **float32 remains the default; quantization is opt-in.**

## Storage formats (self-describing)

The `entities.embedding` BLOB column stores one of three layouts. Quantized
rows carry a one-byte tag prefix so reads never depend on a store-wide flag —
a mixed corpus (only possible mid-rollback) decodes correctly row by row:

| format | tag | payload | 384-dim size | ratio vs float32 |
|---|---|---|---|---|
| float32 (legacy, default) | none | 4·dim LE f32 bytes | 1536 | 1.00× |
| int8 | `0x01` | f32 LE scale + dim i8 codes | 389 | 0.25× |
| bit | `0x02` | dim/8 sign bits | 49 | 0.032× |

- **int8**: per-vector `scale = max|v|/127` (0 for an all-zero vector),
  `code_i = clamp(round(v_i/scale), −127, 127)`. Decodes to `v_i ≈ scale·code_i`;
  cosine ranking on the approximation tracks the exact ranking (standard
  scalar-quantization behavior).
- **bit**: bit i set iff `v[i] > 0.0`, byte-packed with the SAME rule as
  `db::embedding_signature`, so a bit-stored vector's payload is byte-identical
  to its `emb_sig`. Distance is **Hamming over the stored bits** (in-store
  distance scoring), normalized to `1 − hamming/dim ∈ [0,1]` so it is
  comparable with cosine.
- Decoding is length-validated and fail-closed: a blob matching no known
  layout for the query dim decodes to `None` and the row is dropped by the
  existing dim filter — the same end state as the pre-existing mixed-backend
  dim-mismatch path. Layouts are unambiguous for real dims (384/768/1536):
  `4d`, `5+d`, `1+d/8` never collide; float32 is checked first so a
  hypothetical dim-1-float32 vs dim-24-bit collision resolves deterministically.

## Config flag

`PERSEUS_VAULT_EMBEDDING_QUANT=none|int8|bit` (CLI: `--embedding-quant
<none|int8|bit>`). Resolution at open against the store's `embedding_format`
record (schema v33):

- **flag unset** → the store record (default `float32` for every pre-#885
  store and every fresh store with no declaration).
- **flag set, store has a record** → must EQUAL the record; a mismatch fails
  closed at startup with the migration hint. Dense recall never starts with a
  mis-decoded format.
- **flag set, no record, store already holds embeddings** → refused (the
  embeddings are float32; migrate with the tool, not a flag flip).
- **flag set, no record, fresh store** → accepted and recorded, so later
  opens resolve consistently without the flag.

Writers (auto-embed worker, `perseus_vault_embed` single/batch) encode in the
resolved format. `emb_sig`/`emb_sig4` are always derived from the source
vector BEFORE quantization, so the #619 prefilter tiers are
format-independent.

## Reindex / migration path (`perseus_vault_embed`)

New optional args on the existing tool (registry count unchanged):

- `quant_mode: "int8" | "bit"` — store-wide conversion of every stored
  embedding (archived rows included) in ONE transaction:
  1. the current float32 column is snapshotted into
     `entities_embedding_snapshot` (created once, never overwritten — a
     reindex after a restore still rolls back to the ORIGINAL bytes);
  2. every float32 blob is converted in place (pure function — no embed
     model needed); rows that are not legacy float32 are counted as
     `skipped`, never mangled;
  3. in bit mode `emb_sig` is rewritten to the new payload (it is the same
     sign bits) so the v18 "embedded ⟺ signed" invariant holds by
     construction;
  4. the `embedding_format` record and the in-process format mirror flip
     atomically with the commit.
  Refused when the store is already quantized (quantized→quantized is lossy)
  and when the target is `float32` (quantization is lossy — the return path
  is the snapshot restore, or a full re-embed from entity text).
- `restore_quantized_backup: true` — rollback: restore the `embedding`
  column from the snapshot and flip the record back to float32. **Byte-
  lossless** for every row that existed at quantization time (the exact
  original bytes come back — verified by the roundtrip test). Rows written
  AFTER the snapshot keep their quantized form (their float32 never
  existed); they are counted in `still_quantized` and remain fully
  searchable via per-row tag decode.
- `drop_quantized_backup: true` — drop the snapshot after the operator has
  verified the new mode. Irreversible (rollback then requires a full
  re-embed from entity text).

### No-data-loss contract

Nothing is ever deleted or left unsearchable: entities, bodies, sigs and the
prefilter tiers are untouched by conversion; the quantized blobs themselves
decode and score in every mode (mixed corpora rank coherently — cosine in
[0,1] and Hamming similarity in [0,1]); the pre-quantization float32 bytes
live in the snapshot until the operator explicitly drops them. Rolling the
BINARY back to a pre-#885 build is unsupported while the store is quantized
(an old binary would read tagged blobs as f32 and drop them on the dim
filter); restore first.

## Dense-arm behavior

- **float32 / int8**: unchanged pipeline — prefilter on `emb_sig`, fetch the
  pool, score by cosine on the decoded vector (int8: the scale·code
  approximation).
- **bit**: the stored bits ARE the vectors, so the pipeline becomes
  full-corpus and Hamming-only:
  - the resident sig cache is forced ON (unless explicitly disabled), so the
    prefilter covers 100% of the corpus — no #619 recall cliff at scale;
  - the phase-2 pool is the WHOLE corpus (49-byte rows; ~5MB page-cached at
    100K — the flat-recall design, not the pool-truncated approximate one);
  - phase 2 scores the stored bits by Hamming similarity — no full-precision
    vector is ever read or decoded.
- Mixed rows (rollback leftovers) score each by their own metric; the
  dim-mismatch empty-result log is unchanged.

## Acceptance criteria → evidence

| criterion | mechanism |
|---|---|
| quantized storage ≤ 1/8 of float32 bytes (bit mode) | bit layout = 1 + dim/8 bytes: 49 B vs 1536 B (1/31) at 384-dim; asserted by `quantized_reindex_roundtrip_is_byte_lossless` (bytes_per_vector_after = 49) and the measured benchmark below |
| measured recall parity within a defined tolerance | benchmark below (recall@k delta vs float32) + `int8_mode_keeps_recall_parity_on_fixture_corpus` (≥4/5 of f32 top-5) |
| config flag + reindex/migration path from float32 | `PERSEUS_VAULT_EMBEDDING_QUANT` / `--embedding-quant`; `quant_mode`/`restore_quantized_backup`/`drop_quantized_backup` on `perseus_vault_embed`; `set_embedding_quant_declares_fresh_store_and_fails_closed_on_existing` |
| no data loss on rollback | snapshot + `quantized_reindex_roundtrip_is_byte_lossless` (byte-identical restore of all 50 rows), `restore_keeps_post_snapshot_rows_searchable_and_reported` |
| benchmarked: storage bytes/vector, recall@k delta, latency delta | benchmark section below (measured on the synthetic corpus; harness numbers in `benchmark/` unchanged — the memory-quality suite runs in default float32 mode) |

## Determinism anchors

- The fixture generator (`seeded_vec`) is a fixed-seed LCG (no `rand`
  dependency): `s' = s·6364136223846793005 + 1442695040888963407`,
  `x = ((s' >> 33) as u32 / 2^31)·2 − 1`. NOTE: the top 31 bits must be
  scaled by 2^31, not u32::MAX — scaling by u32::MAX collapses every fixture
  vector to negative-only (all-zero sign bits).
- Sign-bit rule shared by `embedding_signature` and `quantize_bit`:
  bit i set iff `v[i] > 0.0` — a bit payload IS an `emb_sig`.
- Decode priority: float32 layout first, then tagged int8, then tagged bit.
- All reindex/restore operations run in one `BEGIN IMMEDIATE` transaction —
  a crash mid-migration rolls back to the pre-migration state (SQLite
  atomicity); no partial formats are observable.

## Compatibility

- Schema v32 → v33: two new tables (`embedding_format`, id=1 record;
  `entities_embedding_snapshot`), created idempotently, no backfill.
- Additive tool surface: three optional `perseus_vault_embed` args; registry
  tool count unchanged (100).
- Default behavior byte-identical: float32 blobs, decode path, and dense
  results are unchanged when no flag is set (the prefilter path, pool sizes,
  and cosine scoring are untouched in f32 mode).
- Rollback of the flag without the tool is refused at open (fail-closed) —
  the migration path is the tool, by design.

## Benchmark (measured 2026-08-09, synthetic corpus, debug build)

Method: 3000-row corpus (dim 384, 10 topic clusters × 300 members =
centroid + 5% seeded LCG noise, L2-normalized); 10 queries = each cluster's
centroid + noise (an unseen member). Ground truth = cluster membership
(recall@k = fraction of top-k from the query's own cluster — a stricter
metric than overlap with the float32 ranking, since every mode is compared
against the same semantic truth). Storage bytes/vector read from the stored
column; latency = wall time of `dense_search(q, 10)` per mode, warm,
single process (p50 of 10 queries). Probe: `vector_quant_bench_probe`
(`#[ignore]`d test; seeds: clusters=50_000+i, members=60_000+c·1000+m,
queries=70_000+c).

| metric | float32 | int8 | bit |
|---|---|---|---|
| storage bytes/vector | 1536 | 389 | 49 |
| storage ratio vs float32 | 1.00× | 0.25× | 0.032× |
| recall@5 (cluster truth) | 1.000 | 1.000 | 1.000 |
| recall@10 (cluster truth) | 1.000 | 1.000 | 1.000 |
| latency p50 (ms/query) | 26.15 | 17.74 | 22.72 |
| latency delta vs float32 | — | −32% | −13% |

Tolerance (defined): recall@k ≥ 0.95 for both quantized modes on this
corpus — both measure 1.000 (bit keeps the exact top-k even at 1/32 the
bytes; int8 is exact on this corpus). Latency: both quantized modes are
faster than float32 on this corpus (bit reads the whole 49-byte corpus
≈147KB page-cached vs the float32 pool's 512×1536B ≈786KB; int8 decodes a
512-row pool). The `benchmark/quality` harness is untouched and passes
60/60 in default float32 mode.
