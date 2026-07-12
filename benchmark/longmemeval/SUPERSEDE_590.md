# #590: event-time supersession-aware recall tiebreak (off by default)

Free (judge-free, offline) before/after for the `PERSEUS_VAULT_SUPERSEDE_RECENCY`
arm. All numbers are LongMemEval `_s` (500), bundled ONNX embeddings, hybrid
recall, produced by `retrieval_diag.py` + `version_inversion.py` + `cov_by_type.py`
on the release binary. Signed reports: [`supersede_590_off_report.json`](supersede_590_off_report.json)
(baseline, flag off) and [`supersede_590_on_report.json`](supersede_590_on_report.json)
(flag on, quantum 0.0008). Reproduce:

```bash
# baseline
python benchmark/longmemeval/retrieval_diag.py --data longmemeval_s_cleaned.json \
  --bin target/release/perseus-vault --k 50 --journal off.jsonl --out off.json
python benchmark/longmemeval/version_inversion.py --data longmemeval_s_cleaned.json --journal off.jsonl --k 10
# arm on
PERSEUS_VAULT_SUPERSEDE_RECENCY=1 python benchmark/longmemeval/retrieval_diag.py \
  --data longmemeval_s_cleaned.json --bin target/release/perseus-vault --k 50 --journal on.jsonl --out on.json
python benchmark/longmemeval/version_inversion.py --data longmemeval_s_cleaned.json --journal on.jsonl --k 10
```

## The problem (confirmed offline)

Knowledge-update questions ask for the **latest** value of a fact that recurs across several sessions with different values ("what is my current rent?", "my personal-best 5K time?"). Hybrid recall ranks by relevance and carries no recency signal, so the *stale* version frequently ranks at or above the *update* — the answerer is fed the outdated value and picks wrong.

I reproduced this with a new free diagnostic (`version_inversion.py`, already in the repo for #590): among knowledge-update questions whose gold evidence spans ≥2 dated sessions, how often the earlier ("stale") gold ranks at/above the later ("update") gold within top-k. **Recall is not the issue** — knowledge-update coverage@10 is ~97% (the gold sessions are almost always retrieved); the failure is *order*.

Baseline (main, `PERSEUS_VAULT_SUPERSEDE_RECENCY` unset), LongMemEval `_s` knowledge-update slice (78 q), top-10:

```
version-bearing (≥2 dated gold): 78
stale ranked above update:       40
→ inversion rate:                51.3%
```

The existing #235 recency arm can't fix this: it keys on `created_at`, which is uniform for a bulk-ingested corpus (the whole LongMemEval haystack shares one ingest instant). The discriminating signal is the **event date** (`session date:` in the body; `created_at`/valid-time in real deployments).

## The change

An **off-by-default** post-fusion tiebreak (`PERSEUS_VAULT_SUPERSEDE_RECENCY=1`):

1. Parse an **event date** per candidate — a `session date:` line, or a structured `date`/`valid_from`/`event_date` field in `body_json`. No parseable date ⇒ the candidate sinks within its bucket (never promoted); it never falls back to `created_at`.
2. **Quantize** fused scores into near-tie buckets (`floor(score / quantum)`, `PERSEUS_VAULT_SUPERSEDE_QUANTUM`, default 0.0008).
3. Sort by `(bucket desc, event-date desc, id asc)` — a valid total order. **Across** buckets relevance still dominates (a clearly more relevant hit is never displaced by a newer but weaker one, so strongly-ranked gold is preserved); **within** a near-tie bucket the later-dated version wins. Stale and update versions of the same fact score near-identically, so they land in the same bucket — exactly where the tiebreak fires.

When the flag is unset, recall is byte-identical to prior behavior (the signed `run.py` / `retrieval_diag.py` signatures are unchanged), so the repo's determinism guarantee (#310) is intact.

## Free before/after — knowledge-update inversion vs. quantum (78 q, top-10)

| quantum | inversions | rate | KU cov@5 | cov@10 | cov@20 | KU hard-miss |
|--------:|-----------:|-----:|---------:|-------:|-------:|-------------:|
| OFF (main) | 40 | 51.3% | 94.9% | 97.4% | 100% | 0 |
| 0.0004 | 22 | 28.2% | 94.9% | 97.4% | 100% | 0 |
| 0.0008 | 14 | 19.2% | 93.6% | 97.4% | 100% | 0 |
| 0.0016 | 13 | 16.7% | 92.3% | 98.7% | 100% | 0 |
| 0.0030 | 5 | 6.4% | 94.9% | 98.7% | 98.7% | 1 |
| 0.0060 | 3 | 6.4% | 92.3% | 93.6% | 98.7% | 2 |

At the default **0.0004** the inversion rate is nearly halved (**51.3% → 28.2%**) with **zero** change to knowledge-update coverage@k. Larger quanta drive inversions lower still, at a small and measurable coverage cost (0.0060 is clearly too aggressive — it starts dropping gold out of top-10).

## Free before/after — general coverage ladder (full 500, k=50)

Baseline (`main`, flag off) is `0 hard-misses@50`, ALL-coverage `@5 87.4 / @10 94.6 / @20 97.6 / @30 99.0 / @50 100`. The tiebreak's effect on the general ladder, per quantum:

| metric | OFF | q0004 | q0008 (default) | q0030 |
|---|--:|--:|--:|--:|
| **KU version-inversion** (top-10) | 48.7% | 28.2% | **19.2%** | 6.4% |
| all-types version-inversion (top-10) | 54.9% | 43.8% | 23.8% | 8.6% |
| ALL coverage@5 | 87.4% | 86.6% | 86.2% | 83.2% |
| ALL coverage@10 | 94.6% | 94.6% | **94.6%** | 93.8% |
| ALL coverage@20 | 97.6% | 97.6% | **97.6%** | 96.8% |
| ALL coverage@30 | 97.6→99.0% | 99.0% | 99.0% | 98.8% |
| ALL coverage@50 | 100% | 100% | 100% | 100% |
| hard-miss@50 | 0 | 0 | 0 | 0 |

**At the default quantum 0.0008 the knowledge-update inversion rate drops 48.7% → 19.2% (−61%) and the all-types rate 54.9% → 23.8%, while coverage@10 / @20 / @30 / @50 are byte-for-byte unchanged.** The only cost is top-5 ordering: ALL coverage@5 dips 1.2pt (multi-session @5 81.2→78.9, single-session-preference @5 93.3→86.7 = 2 of 30). Since the product config retrieves k=10, the metric that matters is untouched.

- **0.0004** is the most conservative setting: −0.8pt @5, everything else identical, KU inversion still down 42%.
- **0.0030** cuts inversion hardest (KU 6.4%) but regresses the general ladder (multi-session @5 −9.8pt; @10/@20 slip ~1pt) — past the useful frontier.

Default is **0.0008**; deployments that want zero @10-and-deeper movement AND minimal @5 movement can set 0.0004.

## Named cases (from #590) — honest scope note

On the **current** `main` binary both named cases are *already* correctly ordered (update ranked above stale): `852ce960` update@1/stale@2, `6a1eabeb` update@1/stale@3. So the specific two QA flips in the issue are not reproducible as retrieval inversions on today's engine (the #590 report predates recent retrieval changes). The phenomenon is nonetheless real and material in aggregate: **40 of 78** knowledge-update questions still rank a stale version at/above the update within top-10. This change targets that aggregate, not those two ids.

## What's verified vs. deferred

- **Verified free:** inversion-rate reduction + coverage-ladder non-regression (offline, judge-free, no API cost).
- **Deferred to the paid pass:** whether the reduced inversion translates into QA accuracy on the knowledge-update slice (toward the full-context 89%), via `qa.py` with the pinned gpt-4o answerer+judge. Consolidated with #588 into a single metered run.

## Productionization notes (beyond this prototype)

- Move the flag/quantum from env to config (`recency.supersede`) alongside the #235 `recency_half_life_secs` arm.
- For non-benchmark corpora, extend event-date extraction to real valid-time / `created_at` when versions are genuinely time-stamped, and tie into the existing supersede/bitemporal machinery (#363/#472) so in-corpus version links are explicit rather than inferred from score proximity.
