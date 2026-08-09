# Source-Chunk Expansion (#888)

Distilled facts resolve back to the **verbatim span** of the retained
transcript they were distilled from — Hindsight-style chunk linkage, with
the Vault's bi-temporal and integrity semantics on top.

## Motivation

Competitive scan 2026-08-07 (Hindsight chunks): each distilled memory links
back to the raw text that generated it; `include_chunks=True` returns
verbatim source material under its own token budget, preserving nuance
("Alice prefers Python" vs the full quote about why). The Vault keeps full
transcripts via capture/ingest_file; distilled facts now carry a source
reference (source id + char span) and an `expand` operation returns the
verbatim span under a token budget, bi-temporal (`as_of` returns the span as
it existed at the fact's capture time).

## Contract

### Capture retention (write side)

`perseus_vault_capture` gains `retain_transcript` (default **true**; skipped
under `dry_run`; no-op on empty payloads):

- One durable `transcript` entity per payload:
  - `category: "transcript"`, `entity_type: "document"`, `source: "capture"`,
    `layer: "buffer"`, tags `["transcript", "capture"]`.
  - `key`: `transcript-<fnv1a-64hex(payload)>` — identical payloads
    re-captured update the SAME row (anti-flood, like notes).
  - `body`: `{"content": <full payload>, "source_path": <source_file|null>,
    "distiller": <rule_based|llm>, "captured_at_unix_ms": <ms>,
    "chunk_hashes": {"<start>:<end>": "<sha256 of verbatim span>", ...}}`.
- Every spanned note (rule-based distiller) carries a minimal
  `source_chunk` pointer in its body:
  `{"source_category": "transcript", "source_key": <transcript key>,
    "span": {"start_char": <char>, "end_char": <char>}}`.
- The report gains a `transcript` block: `{"retained": bool, "id", "key"}`.

Design constraints (measured, see Benchmark):

- **Note bodies stay dedup-neutral.** Capture flood control (#520) is the
  0.7 trigram-Jaccard near-dup merge. A 64-hex `span_sha256` + random ids +
  timestamps per note push capture-note similarity below threshold
  (measured 0.652 on the repo's own near-dup fixture vs 0.838 with the
  minimal pointer) — sibling rows would flood on re-capture. Therefore:
  - the note pointer contains ONLY deterministic values (no ids, no hashes,
    no timestamps);
  - the span hashes live in the **transcript's `chunk_hashes` manifest**
    (the bi-temporal retained store — the correct home for the integrity
    anchor).
- **Char offsets, never byte offsets.** Spans are char-indexed so slicing is
  safe on any UTF-8 (verified with multibyte fixtures).

### Expand (read side)

`perseus_vault_expand_source` (read-only; in the authority read-tool scope
list). Two modes:

- **Fact mode** — `category` + `key` of a distilled fact: reads the fact's
  `source_chunk`, resolves the transcript, returns the verbatim span.
- **Explicit mode** — `source_category` + `source_key` + `start_char` +
  `end_char` (+ optional `span_sha256`): expands an arbitrary span of any
  retained source.

Common args: `max_chars` (1..=16384, default 2000; longer spans truncate
with `truncated: true`), `as_of_unix_ms` (bi-temporal anchor; **defaults to
the fact's `created_at_unix_ms`** — the span as it existed at capture
time), `workspace_hash` (permission scope).

Contract block:

```
{
  "status": "expanded" | "no_source_ref" | "fact_not_found"
          | "source_missing" | "span_invalid",
  "source":    {id, category, key, entity_type, source, created_at_unix_ms},
  "fact":      {category, key},            // fact mode only
  "span":      {start_char, end_char, chars},
  "span_sha256": "<sha256 of returned text>",
  "verification": "ok" | "unchecked",
  "as_of_unix_ms": <anchor used>,
  "text": "<verbatim span, budget-truncated>",   // absent unless expanded
  "truncated": bool,
  "budget":    {max_chars, chars, span_chars},
  "excluded": [<reasons>]
}
```

Status semantics (never errors — graceful outcomes):

| status | meaning | excluded reasons |
|---|---|---|
| `expanded` | verbatim span returned, integrity verified | — |
| `no_source_ref` | fact has no `source_chunk` (API writes, LLM-distilled notes, `retain_transcript: false`) | `fact_body_has_no_source_chunk_reference`, `source_chunk_missing_category_or_key` |
| `fact_not_found` | fact entity absent | — |
| `source_missing` | transcript absent (incl. `as_of` before capture) or body has no `content` | `retained_source_not_found`, `retained_source_body_has_no_content` |
| `span_invalid` | span out of bounds, or hash mismatch — **fail-closed, no text** | `span_out_of_bounds_of_retained_source`, `retained_source_changed_since_capture` |

Integrity: the expected hash is the source body's
`chunk_hashes["<start>:<end>"]` (capture-stamped), or the caller-supplied
`span_sha256` in explicit mode. The hash of the extracted verbatim text must
match. `verification: "unchecked"` only when NO expected hash exists (e.g.
explicit mode without `span_sha256`, or a transcript rewritten by an
operator without the manifest) — the hash is then reported as computed.

Bi-temporal semantics (via the existing `Database::as_of` over
`entity_history`):

- Default anchor (fact capture time) always resolves the span **as it
  existed when the fact was distilled** — even after the transcript was
  later edited.
- A later anchor resolves the current/historical body; if the content
  changed, the manifest hash mismatches → `span_invalid` (fail-closed).
- An anchor before the transcript existed → `source_missing` (honest
  absence, not a stale read).

Determinism anchors: identical payload → identical transcript key, identical
spans, identical manifest hashes, identical `source_chunk` pointers
(no random content in note bodies; verified by
`capture_spans_are_deterministic_and_utf8_safe`). `as_of_unix_ms` is echoed
verbatim.

## Migration / back-compat

- **No schema change, no migration, no reindex.** Source refs live in
  entity bodies; transcripts are ordinary entities.
- Existing stores are unaffected: retention only starts on new capture
  calls; pre-existing notes have no `source_chunk` and expand to
  `no_source_ref` gracefully.
- `retain_transcript: false` restores the pre-#888 write shape exactly
  (no transcript entity, no refs).
- Registry: +1 tool (`perseus_vault_expand_source`) → 109 canonical.
  Metadata counts synced (README, CLAIMS-AUDIT, manifest.json, server.json,
  glama.json, mcp.rs test).

## Benchmark (measured, 2026-08-09)

Probe: `expand_source_latency_probe` (`#[ignore]`, deterministic template
corpus in `src/tools.rs`), run on Greg (Unraid, 32GB; binary
`/opt/data/cargo-target-nofeat/debug/perseus-vault`). 500 fact-mode expands
(verification on) against a 1.74 MB transcript (100 headed sections), 200
against a ~4 KiB transcript.

| metric | value |
|---|---|
| expand p50 latency, ~4 KiB transcript (fact mode, verified) | **1.3 ms** |
| expand p50 latency, 1.74 MB transcript (fact mode, verified) | **147 ms** |
| note-body storage overhead (minimal `source_chunk` pointer) | **96 B/note** |
| transcript retention overhead | payload bytes + ~100 B + ~70 B/note manifest |
| capture-note dedup Jaccard (repo near-dup fixture) | **0.838** ≥ 0.7 threshold (0.826 pre-#888) |

Latency note: the 1.74 MB case is dominated by the pre-existing governance
suppression digest (`filter_suppressed` normalizes + hashes the WHOLE entity
body per read — the same cost any `get_entity`/`as_of` of a large entity
already pays, not an #888 regression). Typical transcripts (KB-scale) are
~1 ms. Span extraction + SHA-256 themselves measure single-digit µs.

Numbers are from the probe run recorded in the PR evidence; the corpus is
deterministic template text, so the run is reproducible.

## Pitfalls

- **Do not put per-note random/hash content in note bodies.** The 0.7
  capture dedup is the flood control; measured 0.652 < 0.7 with full
  chunk metadata in-body (see above). Hashes belong in the transcript
  manifest.
- Spans are char offsets — never slice bodies with byte indices
  (multibyte content).
- `as_of` defaults to the fact's creation time BY DESIGN (issue
  requirement); current-state reads must pass an explicit anchor.
- `#[serde(default)]` on `max_chars` yields `usize::default()` (0), not the
  documented 2000 — use `default = "default_expand_budget"` with
  `deserialize_with`.
