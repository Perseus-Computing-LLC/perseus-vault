# Sleep-cycle consolidation: dedup + contradiction resolution (#1002)

Status: normative. Surface: `perseus_vault_sleep`, `sleep` lane of
`perseus_vault_operator_review`, `sleep_proposal.*` state keys.

Borrowed from CogniCore's SleepProcessor (off-peak dedup, jaccard+negation
contradiction detection, episode compression). The vault version reuses the
existing bounded primitives instead of new scan machinery, and it is
PROPOSAL-ONLY where CogniCore acts:

## Phases (one bounded scan)

1. **Dedup** — pairwise trigram similarity ≥ `similarity_threshold` within
   the scan window → `kind=merge` proposal.
2. **Contradiction** — the cheap CogniCore prefilter: ≥2 shared non-trivial
   tokens AND a negation word/phrase (token-matched — "nothing" never
   false-positives on "not"; phrases like "must not"/"no longer" matched as
   bigrams) → `kind=conflict` proposal. Negation-shaped pairs are
   classified BEFORE the similarity test, so a high-similarity
   "X works" / "X does not work" pair is NEVER proposed as a merge.
3. **Compression** — optional delegated `consolidate(cold_first)` over the
   same category: the only auto-committed artifact, and it already emits
   evidence-linked observations with supersedes links while exempting
   verified/scored sources. Runs in its own maintenance slot (the sleep
   slot is released first — nested acquisition fails-early by design).

## Guards

- **Never silent**: merge/conflict findings are proposals persisted under
  `sleep_proposal.<uuid>` state keys (zero schema change) and rendered as
  the `sleep` lane of the operator review queue; each table row names its
  decision path (`merge`/`verify`/`forget`/`state_delete`). Nothing is
  auto-merged or auto-resolved.
- **Bounded**: #952 maintenance gate (off-peak window + live-recall SLO
  start gate + serialized execution slot; `force` for explicit operator
  runs), `max_entities` scan budget, `max_proposals` budget.
- **Dry-run purity**: `dry_run=true` performs the identical scan with zero
  writes (proposals + compression).
- **Scope**: #854 workspace scoping + capability-gated global mode;
  curated `mental_model` category refused (#886).

## Evaluation

The #916 MemConflict-style set is the natural harness: sleep recall and
contradiction precision over injected negation pairs, with false-positive
("nothing" substring) canaries in the prefilter unit tests.
