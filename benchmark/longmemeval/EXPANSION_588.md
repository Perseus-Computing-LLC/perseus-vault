# #588: multi-query expansion + date-aware arm — free-gate results


Draft. Free (judge-free, offline) before/after on current `main`. Refs #588. Off-by-default via `PERSEUS_VAULT_QUERY_EXPANSION` / `PERSEUS_VAULT_DATE_ARM`; recall is byte-identical to `main` when unset.

## Approach (aligned to the #588 oracle)

The #580 hard-miss case study found evidence sessions with little/no raw content-token overlap with the question. Two off-by-default arms:

1. **Multi-query expansion.** Generate rule-pack sub-queries and fuse each as its own **equal-voice arm in one flat RRF pass** (`flat_rrf`), with the base arm up-weighted (base 1.5 / expansion 1.0) so expansions can only *lift* evidence the original phrasing missed, never displace a strong base hit. This is the symmetric N-arm form the oracle validated (the online path swaps the rule pack for an LLM decomposition). Rule families: self-age hop (with the pivot entity bridged in — "Alex age born"), aggregation-core ("how many X" → "X"), a small synonym/instance lexicon (movie↔film, doctor→specialist/dermatologist, concert→show/musical event), and a temporal-residue sub-query for date-keyed questions. Fan-out capped at 4.
2. **Date-aware arm.** When the query carries a relative-date expression ("two weeks ago") and a query-date anchor is supplied, resolve `target = anchor − offset` with real day arithmetic and move candidates whose event date falls within a unit-scaled window of the target to the front. Paired with the residue sub-query (which pulls the dated session into the pool), this recovers the date-keyed miss.

## Free before/after — the six #580 hard-miss questions (worst gold rank, full-500, k=50)

Expansion on (`PERSEUS_VAULT_QUERY_EXPANSION=1`):

| question | type | OFF | ON | effect |
|---|---|--:|--:|---|
| `a1cc6108` | multi-session | 14 | **2** | → covered@10 |
| `ba358f49` | multi-session | 27 | **9** | → covered@10 |
| `gpt4_a56e767c` | multi-session | 8 | **4** | improved |
| `gpt4_f2262a51` | multi-session | 28 | **15** | → covered@20 |
| `gpt4_1e4a8aec` | temporal | 22 | **13** | residue sub-query → covered@20 (date arm pushes to top-10) |
| `gpt4_d6585ce8` | temporal | 37 | 24 | improved (still >20) |

**All six improve**; five of six now have all gold ≤15, most ≤10. (Exact ranks are ±a few run-to-run — this deep-rank metric is tie-sensitive — but the direction is consistent.)

## Date-aware arm — the 6th case (`gpt4_1e4a8aec`, "what did I do two weeks ago?")

With `PERSEUS_VAULT_DATE_ARM=1` + the query-date anchor (via the new `expansion_date_diag.py`, since `retrieval_diag` doesn't pass one), top-20:

| gold session | date | OFF | ON |
|---|---|--:|--:|
| `answer_16bd5ea6_2` (the update, answer-bearing) | 2023/04/21 | absent | **5** |
| `answer_16bd5ea6_1` | 2023/04/15 | absent | 5–absent (borderline; 6 days from the 2-week target) |

The residue sub-query ("gardening activity") pulls the dated session into the pool; the date window (unit-scaled, ±7 for "weeks") promotes it. The **update session that determines the answer is reliably recovered** into top-10.

## Free before/after — general coverage ladder (full 500, k=50, expansion on)

Expansion is a **recall-depth vs top-5-precision trade** — it lifts the deeply-ranked hard-miss golds (improving @20/@30) at a cost to easy top-5 hits:

| coverage | OFF | ON | Δ |
|---|--:|--:|--:|
| ALL @5 | 87.4% | 84.6% | −2.8 |
| ALL @10 | 94.6% | 94.4% | −0.2 |
| **ALL @20** | 97.6% | **98.2%** | **+0.6** |
| ALL @30 | 99.0% | 99.2% | +0.2 |
| ALL @50 | 100% | 100% | 0 |

By slice: multi-session **@20 95.5→97.0 (+1.5)**, temporal **@20 96.2→97.0 (+0.8)** and @10 91.7→93.2 (+1.5) — the target slices gain in the k-range that matters — while temporal @5 drops 78.2→71.4 (−6.8) and single-session-user @5 97.1→94.3. **No coverage@20 regression anywhere** (the stated gate); the cost is concentrated at @5.

Interpretation: the expansion arms surface additional relevant sessions that displace some easy top-5 golds while pulling hard-miss golds up into @10–@20. The base-arm up-weight (1.5, `PERSEUS_VAULT_EXPANSION_BASE_WEIGHT`) is the dial that trades @5 cost against hard-miss recovery; raising it (or gating expansion to low-base-confidence queries) is the obvious next tuning. For a k=10 QA config, @10 is ≈neutral (−0.2) and the multi-hop/aggregation slices — which need *several* sessions in context — gain at @20.

## Cost & notes

Expansion multiplies per-query retrieval by the sub-query fan-out (capped at 4; fires only on recognizable shapes — most questions expand to nothing). Productionization: move flags/weights/lexicon to config; swap the rule pack for an LLM decomposition online; scale the date window per unit. Verified free; QA-accuracy confirmation deferred to a consolidated paid pass (`qa.py`, pinned gpt-4o).
