# Deterministic fingerprint tier (zero-API fallback semantic hashing)

Status: implementation specification
Date: 2026-08-13
Resolves: #1020
Related: `vector-compression.md` (#885, the bit/Hamming scoring and bounded-scan
shapes this tier reuses), insight `public-repo-comparison-hillock-perseus`
(competitive scan: Hillock v0.4.1 subword-HDC reservoir — verified locally,
2026-08-13), insight `public-repo-comparison-memu-perseus` (offline-embeddings
lesson: `doctor`/`retrieve` fail without an embedding key even in "local" mode),
insight `mnemosyne-hindsight-differentiation-2026-08-07` (Mnemosyne MIB
candidate — bit/Hamming vectors in SQLite, of which #885 shipped the
quantization half and this tier ships the deterministic-hashing half).

## Motivation

Embeddings currently require a live backend: the bundled ONNX model, a local
model file, or a remote endpoint. When none is available, dense recall fails
outright (the #226 contract: error, never silent FTS degradation). The memU
scan shows the failure mode in the wild — "local" memory that still needs an
API key to retrieve. This spec adds a **deterministic, GPU-free, zero-API
fallback ranker**: a subword-HDC fingerprint of the plaintext body, stored
with the entity, ranked by popcount/Hamming when the embedding backend is
unavailable. Degraded-but-functional semantic-ish recall with zero external
calls; deterministic replays; OOV/name matching by construction. It is
**never primary**: with a working embedding backend, dense recall is unchanged.

## Algorithm (Hillock `SubwordHDCEncoder` mirror)

For a text `t`:

1. `padded = "#" + lowercase(trim(t)) + "#"` (char boundaries, like Python
   str indexing; hash input is the UTF-8 encoding).
2. For `n` in `{3, 4, 5}`: slide an `n`-char window across `padded`; each
   window is an *n-gram*.
3. Each n-gram maps to a deterministic random ±1 vector in 10,000-dim
   bipolar space: seed = FNV-1a 64-bit over the n-gram's UTF-8 bytes, bits
   from a splitmix64 stream (bit `i` is stream word `i/64`, bit `i%64`).
   Every n-gram vector is therefore a pure function of the n-gram.
4. Accumulate all n-gram vectors into an `i32[10000]` counter array
   (+1/-1 per dimension), then threshold: bit `i` set iff `count[i] >= 0`
   (Hillock's sign rule with zeros resolving to +1).

Similarity is `1 − hamming/dim ∈ [0,1]` — the same metric and range as
`vector_quant::bit_similarity` (#885), so fingerprint scores are
rank-comparable with dense scores. An unrelated text pair sits at the bipolar
noise floor **~0.5** (σ ≈ 0.005 at 10k dims), not 0.

Deliberate differences from Hillock (documented in `src/fingerprint.rs`):

- **Bit source**: FNV-1a-seeded splitmix64 instead of numpy MT19937.
  Byte-identity with Hillock is not a goal; determinism *within this binary*
  is — no RNG state, no HashMap iteration, no floating point, platform-free.
- **Empty/short text** yields the all-+1 vector (the zero-count sign rule),
  not Hillock's unseeded random draw.
- **No GloVe-50d half**: the sign-random-projection over GloVe needs a
  ~10MB external vocabulary file, which contradicts the zero-API, no-external
  data-files premise. Subword-only is the deterministic OOV-robust core.
- Unicode case folding follows the Rust std tables; identical input is
  byte-identical within one build, and Unicode-table revisions across Rust
  releases may shift bits (a hash, not a contract).

## Storage

`entities.fingerprint BLOB NULL` (schema **v43**, additive migration,
backfill-free):

- `FINGERPRINT_DIM = 10,000` (Hillock's `HDC_DIMENSION`), packed as
  1,250 bytes per entity (1 bit/dim).
- **Written on content change when the tier is enabled** (create and update
  paths, computed inline in the write transaction from the same plaintext
  the auto-embed worker consumes). No queue, no backend, no I/O.
- **Cleared on content change when the tier is disabled** — the same rule as
  the embedding clear: a fingerprint for a body the row no longer has is
  wrong in any configuration, so a later re-enablement can never serve a
  stale fingerprint.
- Rows written before enablement carry `NULL` and stay out of the fallback
  pool until their next content change. No backfill in this iteration (see
  Follow-ups).

## Config

`PERSEUS_VAULT_EMBEDDING_FINGERPRINT=on|off` (CLI: `--embedding-fingerprint
<on|off>`), default **off**. Accepts `1/true/on/yes` and `0/false/off/no`;
an invalid non-empty value is an open-time error (fail-closed, matching the
quant flag's strictness). The flag also appears in the config self-report
(`fingerprint_tier` stage, #1010 pattern).

Unlike quantization there is **no store record**: fingerprints are a
deterministic function of text and the fixed 10k layout, so nothing already
stored can be mis-decoded, and flipping the flag on any store is safe.
Enablement covers writes from this process on; disablement stops storing.

## Retrieval semantics

Engagement is gated twice: the tier flag **and** the embed error path.

- A dense/hybrid recall with a working embedding backend never consults
  fingerprints. The fallback engages only when query embedding fails
  (`generate_embedding_with_fallback` errors) while the tier is on.
- With the tier **off**, the existing #226 error contract is preserved even
  if the store contains fingerprints (tested).
- The fallback ranks with `fingerprint_search_bounded` — a mirror of
  `dense_search_governed_scan`: the same `PERSEUS_VAULT_DENSE_MAX_SCAN`
  ceiling, governance suppression respected (the interceptor filters before
  scoring), score desc + id asc tie-break, truncate to limit. Completeness
  semantics are the same as the dense arm (exact/bounded/partial).
- Hybrid mode fuses the fingerprint arm with the FTS arm exactly like the
  dense arm (same RRF path); the per-arm telemetry audit records the arm as
  `fingerprint` so fallback ranking is distinguishable in `recall_arm_audits`.
- Foreign-length blobs (not 1,250 bytes) are dropped, never scored
  (fail-closed, same end state as the dense dim filter).
- The auto-embed settle/flush step is skipped in fallback mode (no backend
  to settle against).

## Measured behavior

Probe: `cargo test --no-default-features popcount_comparison_probe -- --ignored`
(`src/fingerprint.rs`; similarity fixtures pinned by the unit tests). All
numbers measured live; labeled with build and host. Debug numbers below are
from the local dev host (`perseus-vault` worktree, origin/main @ 2894fa7 +
this branch); release numbers are marked in the follow-up run.

| metric | debug (measured) | release (measured) |
|---|---|---|
| sim `Alan_Turing` vs `Alan Turing` (near-miss) | 0.7133 | 0.7133 (bit-identical — determinism cross-check) |
| sim `Alan Turing` vs `Grace Hopper` (unrelated) | 0.4952 | 0.4952 |
| sim unrelated long strings | 0.4977 | 0.4977 |
| hamming compare (1,250-byte pairs) | 13,564 ns/cmp — 92.2 M byte-pairs/s | 530.8 ns/cmp — 2,355 M byte-pairs/s |
| encode (write-path cost, ~70-char bodies) | 15,492 µs/encode | 1,070 µs/encode |

Derived (labeled, not measured as a unit): at the release compare rate a
full-corpus fallback scan costs ~0.53 µs × corpus size — ~53 ms at 100k
fingerprinted rows, linear by construction (same cost class as the #885
bit-mode scan).

The noise floor is ~0.5 with σ ≈ 0.005 (10k independent bits), so a near-miss
at 0.713 is ~40σ above noise — the separation the acceptance test asserts
(near-miss > 0.55 and > unrelated + 0.03).

## Acceptance coverage

- **Determinism**: same input → identical bytes, 20 repeated encodes
  (`deterministic_same_input_identical_bytes`).
- **Binding-orthogonality analog**: unrelated pairs sit at the noise floor
  (±0.02) (`unrelated_texts_sit_at_the_noise_floor`).
- **Near-miss spelling robustness**: `Alan_Turing` vs `Alan Turing` > 0.55
  and clearly above an unrelated pair (`near_miss_spelling_stays_above_noise_and_above_unrelated`).
- **Storage cost**: exactly 10,000 packed bits / 1,250 bytes
  (`storage_cost_is_exactly_10k_packed_bits`).
- **Popcount benchmark**: the probe above.
- **Fallback path with embeddings disabled still returns ranked candidates**:
  `fingerprint_fallback_ranks_without_embedding_backend` (near-miss tops the
  ranking), plus hybrid (`fingerprint_fallback_supports_hybrid_mode`).
- **Write-path semantics**: store/recompute/clear
  (`fingerprint_write_path_stores_recomputes_and_clears`).
- **Error-contract preservation**: tier off + fingerprints present → still
  errors (`fingerprint_tier_disabled_keeps_error_contract_despite_stored_fingerprints`).
- **Pool membership**: unfingerprinted rows excluded
  (`fingerprint_fallback_skips_rows_without_fingerprints`).
- **Flag parse**: documented spellings accepted, garbage rejected
  (`parse_fingerprint_flag_accepts_documented_spellings_and_rejects_garbage`).
- Case/whitespace invariance, Unicode determinism, empty/short inputs,
  length-mismatch → score 0 (fail-closed).

## Limitations and non-goals

- **Bag-of-n-grams**: no word-order information beyond 5-char windows; this
  is lexical-adjacent similarity, not semantic similarity. That is the
  contract — "degraded-but-functional", never a replacement for dense search.
- **Dynamic range**: scores cluster near 0.5; ranking is meaningful,
  absolute scores are not thresholds.
- **No GloVe half, no ANN, no learned components** — by design (zero-API,
  deterministic).
- **No backfill**: enabling the tier covers new and changed bodies only
  (see Follow-ups).
- **Write-path cost**: ~3·len n-grams × (157 splitmix draws + 10k adds) per
  write; measured above. Only paid when the tier is on.
- Storage is 1,250 bytes/entity while enabled — an explicit trade, not free.

## Follow-ups

- Batch backfill on `perseus_vault_embed` (fingerprint `WHERE fingerprint IS
  NULL` alongside the `WHERE embedding IS NULL` pass) — filed separately if
  the operator workflow needs it.
- Rolling FNV over sliding windows (encode ~3× faster) if write-path cost
  ever shows up in a profile.
- A labeled recall@k measurement of the fallback against the dense arm on a
  shared corpus, for an honest head-to-head (gauntlet discipline: measured,
  same hardware, or not stated).
