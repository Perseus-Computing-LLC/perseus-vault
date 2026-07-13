# #590 closed out: knowledge-update under PRODUCT-shape ingest (`--ku-shared-key`)

`INGEST_590.md` established that the #590 version-inversion is a benchmark-modeling
artifact: LongMemEval gives every session its own `key`, so all versions of a fact
stay live and compete in recall, bypassing the engine's latest-wins supersede
machinery entirely. Its recommendation — a harness mode that ingests
knowledge-update fact versions the way real callers do — is what this measures.

## The mode (`retrieval_diag.py --ku-shared-key`, `qa.py --ku-shared-key`)

For every version-bearing question (≥2 dated gold sessions), the gold
(fact-version) sessions ingest under **one shared key** with
`valid_from` = session date, ascending — `INGEST_590.md` demo B at benchmark
scale. `mimir_remember` collapses them to a live latest-wins row; stale versions
go to `entity_history` (still exactly recoverable via `mimir_valid_at`). All
other haystack sessions ingest unchanged.

**Grouping honesty:** which sessions update the same fact comes from the
dataset's evidence labels (`answer_session_ids`). That is *authoring-time*
knowledge — exactly what a real caller has when it re-remembers a fact under
its key — and exactly what `SUPERSEDE_590.md` proved is unrecoverable at recall
time (no content/embedding/score signal survives). This mode is therefore a
measurement of the engine's real update semantics, **not** a retrieval
improvement claim on benchmark-shape corpora. Reports/journals pin the shape
(`ingest_shape`, `ku_shared_key` in the config header + signature); never
compare across shapes without labeling both.

## Retrieval (offline, judge-free — 78 knowledge-update questions, k=50)

| metric (top-10) | benchmark shape | ku-shared-key (product) |
|---|---:|---:|
| version-inversion cases (`version_inversion.py`) | 37/78 (47.4%) | **0/78 (0.0%)** |
| latest-version coverage@5 | 96.2% | 96.2% |
| latest-version coverage@10 | **100%** | **100%** |

Reports: `ku590_benchshape_report.json` (sig `0e7aad0f…`),
`ku590_sharedkey_report.json` (sig `adb1d615…`).

Two readings:
- The latest version was **always retrieved** at k=10 in both shapes — #590 was
  never a recall miss. It was the stale co-live version reaching the context
  alongside (or above) the update and the answerer picking wrong.
- Product-shape ingest removes the inversion **structurally**: the stale version
  is not in the live index, so there is nothing to mis-rank. `coverage_at_k`
  (all-gold) reads 0% in this shape *by construction* — stale golds are in
  history deliberately; `coverage_latest_at_k` is the comparable metric.

A real bug fell out of this work: the harness date helper (`to_ms`) dropped the
time-of-day, mis-ordering same-day updates (a 17:45-vs-22:16 version pair
ingested the earlier value as "latest"). Fixed in all three scripts; the
inversion count under the fixed parser is the one above.

## End-to-end QA (78 knowledge-update questions, official-CoT prompt, official
judge, answerer+judge gpt-4o-2024-08-06, temp 0, k=10, same binary, same day)

| arm | ingest shape | knowledge-update accuracy |
|---|---|---:|
| `ku590_qa_benchshape_report.json` (sig `14f01002…`) | unique-key-per-session (benchmark) | 57/78 = **73.1%** |
| `ku590_qa_sharedkey_report.json` (sig `8a3e06e9…`) | ku-shared-key (product) | 61/78 = **78.2%** |

Paired read (same 78 questions): 14 flipped wrong→right, 10 right→wrong, net
+4. Both questions the #749 certification flagged as fullcontext-correct →
vault-wrong (`6a1eabeb`, `852ce960` — the two that opened this issue) are in
the **fixed** column. The ±10–14 churn includes abstention (`_abs`) pairs
flipping in both directions — consistent with normal answerer variance on this
slice (signed full-500 runs put knowledge-update anywhere in a 76.9–80.8%
band across seeds) plus context-composition changes, not with a one-directional
shape effect beyond the net gain.

Reference points (labeled, different runs/scales — do not blend): #749
stratified cert had knowledge-update as the **only** category where
full-context (8/9) beat the product arm (6/9); full-500 CoT primary run scored
knowledge-update 80.8% (pre-#618 base). The 88.9% full-context stratified
figure is n=9 and not directly comparable to this 78-question slice.

## Conclusion

The engine's latest-wins semantics, exercised the way real usage exercises
them, eliminate the #590 failure mode — the version-inversion is 0 by
construction and measured (47.4% → 0.0%), answer accuracy moves +5.1pts
(73.1% → 78.2%), and both certified #749 regressions are fixed. No
default-recall change is warranted (the recall-time approach was already shown
ineffective in `SUPERSEDE_590.md`). The off-by-default
`PERSEUS_VAULT_SUPERSEDE_RECENCY` reranker remains as the documented fallback
for corpora with genuinely unlinked co-live versions. #590 is closed as
characterized: benchmark-modeling artifact, engine semantics correct.
