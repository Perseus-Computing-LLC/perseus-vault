# #590: knowledge-update version-inversion — diagnosis, and why recall-time reranking doesn't fix it

> **Correction (supersedes the first version of this file, merged in #621).**
> The earlier draft reported the recall-time tiebreak cutting knowledge-update
> inversion 48.7%→28.2%→19.2%. Those numbers were measured on a **pre-#618
> base**; re-measured on current `main` the mechanism is **ineffective** (it
> fluctuates around the baseline). This file records the corrected, honest
> result: a confirmed diagnosis + a measured negative result for recall-time
> reranking, pointing the fix to ingest time.

Free (judge-free, offline) investigation of the knowledge-update ordering
problem, on the LongMemEval `_s` knowledge-update slice (78 questions), release
binary, bundled ONNX embeddings; no API key, no network.

## Confirmed diagnosis (order, not recall)

`version_inversion.py` on the 78 knowledge-update questions, top-10:

| metric | value |
|---|--:|
| coverage@10 (gold retrieved) | 97.4% |
| **version-inversion rate** (stale ranked ≥ update in top-10) | **~42–46%** (≈33–40/78) |

Recall is fine — the gold sessions are almost always retrieved; the *update* is
just ordered at/below the *stale* version, so the answerer is fed the wrong
value. (The raw count varies ±2–3 run-to-run; this exact-order metric is
tie-sensitive.)

The `#235` recency arm is inert here: the benchmark ingests every session at one
`created_at` instant, so it can't separate versions. The discriminating signal
is the **event date** in the body (`session date:`), which the prototype parses
at recall time (`entity_event_date_key`).

## Negative result: three recall-time mechanisms, none effective (current `main`)

All are OFF by default; all leave coverage@5/@10/@20 unchanged (94.9 / 97.4 /
100.0 on the slice — regression-free). Inversion count out of 78, top-10:

| mechanism | signal | OFF | on (swept) |
|---|---|--:|--|
| score-bucket near-tie tiebreak | RRF-score proximity | 33 | 35 / 40 / 36 / 34 / 36 (q=4e-4…6e-3) |
| content cluster (union-find) | trigram similarity | 35 | 35 / 33 / 37 / 40 (sim 0.20…0.45) |
| semantic cluster (union-find) | embedding cosine | 35 | 33 / 36 / 36 / 37 / 35 (cos 0.40…0.75) |

**None pushes inversion reliably below the ~33–40 baseline band; several land
above it.** Why each fails on this benchmark:

- **Clustering (content or embedding).** A fact's versions are *different
  conversations* that each update one value — they share almost no content and
  only weak embedding similarity, so union-find on either signal barely links
  the true version pairs (fires on <1–2 questions even at very low thresholds).
- **Score-bucket near-tie.** The tiebreak can only reorder candidates in the
  same quantized score bucket. On current-`main` fusion the stale/update gold
  usually do *not* share a bucket, so it reorders *unrelated* near-ties by date
  — adding noise. (A pre-#618 build did show a clean reduction; #618's retrieval
  changes moved the gold out of shared buckets and it vanished — a caution
  against depending on incidental score proximity.)

Root cause: **recall-time reranking must *infer* which candidates are versions
of the same fact, and on this benchmark there is no reliable recall-time signal
to infer it from** (not content, not embedding, not score proximity).

## Recommendation: fix at ingest time (the issue author's flagged plan)

Versions should be *known*, not guessed:

1. **Engine** — `mimir_remember` accepts an explicit valid-time; recall exposes
   it as a ranking signal (`entity_event_date_key` / `parse_event_date_key` here
   are a first step).
2. **Harness** — pass each session's `session date:` as that valid-time.
3. **Version linking** — link same-fact/same-key sessions through the existing
   supersede / bitemporal machinery (#363/#472) so "latest wins" is a lookup,
   not an inference. (LongMemEval sessions carry unique keys and aren't
   pre-linked, so a benchmark harness must also assign a shared key per fact —
   the grouping problem that defeats the recall-time approaches, moved to where
   the ground truth exists.)

## What ships / stays

- `version_inversion.py` (diagnostic; #593) + `cov_by_type.py` (per-type
  coverage@k) — reusable free-gate tooling.
- `entity_event_date_key` / `parse_event_date_key` — event-date extraction the
  ingest-time fix reuses.
- `supersede_reorder` (off by default, `PERSEUS_VAULT_SUPERSEDE_RECENCY`) — kept
  as a documented, measured dead-end and fallback; `main` behavior is
  byte-identical when unset (sha256 fingerprints intact, #310).

Content-hashed current-`main` slice reports: [`supersede_590_off_report.json`](supersede_590_off_report.json),
[`supersede_590_on_report.json`](supersede_590_on_report.json).
