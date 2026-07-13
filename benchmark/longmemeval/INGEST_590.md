# #590 ingest-time analysis: the engine already does "latest wins" for same-key updates

Following the #590 free-gate negative result (recall-time reranking can't identify
versions on this benchmark — see `SUPERSEDE_590.md`), this is the ingest-time
investigation. **Conclusion: no engine change is warranted.** Perseus's live
index + history/bitemporal/supersede machinery already implements "latest wins"
for genuine same-key updates; #590's version-inversion is an artifact of how
LongMemEval *models* updates.

## The mechanism (already in the engine)

`mimir_remember` on an existing `(category, key)` does a COALESCE **update of the
live row** and **snapshots the prior version into `entity_history`** (db.rs, the
remember/upsert path, #363/#472). It already accepts `valid_from_unix_ms` /
`valid_to_unix_ms`, and `mimir_valid_at` / `mimir_as_of` reconstruct any past
version bi-temporally. Recall runs over the **live index**, so it returns the
single latest version per key — there is no stale row to mis-rank.

## Demonstration (`knowledge_update_semantics_demo.py`, offline, free)

On knowledge-update instance `852ce960` (mortgage pre-approval amount; gold
sessions dated 2023/08/11 and 2023/11/30):

| ingest shape | recall returns |
|---|---|
| **A. unique key per session** (LongMemEval) | **both** versions live → ranking must guess the latest → the #590 inversion |
| **B. shared key + `valid_from` = session date** (real usage) | **only the latest** (2023/11/30); the 08/11 value is in history, recovered exactly by `mimir_valid_at(as-of 08/11)` |

## Why #590's metric shows an inversion anyway

LongMemEval assigns every session its own `key`, so all versions stay live and
compete in recall — bypassing the update semantics entirely. That is the
artifact `version_inversion.py` measures. It does not reflect how Perseus is
used (an updated fact is re-`remember`ed under the same key), where the stale
value is never in the live recall set.

## Recommendation

- **No default-recall change.** Real same-key updates are already correct.
- Treat #590 as a **benchmark-modeling gap**, not an engine bug. To exercise the
  real semantics in the harness, ingest knowledge-update facts under a shared
  key with `valid_from` (as demo B does) — but that requires grouping sessions
  by fact, which the free-gate work showed has no reliable recall-time signal;
  it's only knowable at authoring/ingest time (exactly the point).
- The off-by-default recall-time reranker (`PERSEUS_VAULT_SUPERSEDE_RECENCY`,
  from the merged #590 PR) stays as a documented fallback for corpora that
  genuinely have unlinked co-live versions.
