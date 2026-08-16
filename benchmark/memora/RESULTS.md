# Memora LoCoMo Baseline — Perseus Computing re-run (2026-08-15)

Independent re-run of microsoft/Memora on the LoCoMo benchmark, executed on Perseus
Computing hardware to pin a competitor baseline we own (rather than citing the
paper's published range or third-party claims).

## Conditions

| Item | Value |
|---|---|
| Harness | microsoft/Memora @ `dec3f8f2444eace7004fc084abe1be9f3d88270e` |
| Dataset | `app/locomo/data/locomo10.json` (snap-research/locomo) — 10 conversations, 1,986 QA pairs; cats 1–4 = 1,540 scored |
| Dataset sha256 | `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4` |
| Judge / answerer | gpt-4.1-mini (temperature 0) |
| Retrieval | top_k 30, `force_rebuild: true` |
| Configs | S = semantic retriever (`memora_s_baseline.yaml`) · P = policy/prompted retriever (`memora_p_baseline.yaml`) |

## Results (LLM judge, cats 1–4)

| Config | Overall | Cat 1 (single-session) | Cat 2 (multi-session) | Cat 3 (temporal) | Cat 4 (knowledge) |
|---|---|---|---|---|---|
| Memora S (semantic) | **0.8591** | 0.7908 | 0.8505 | 0.6458 | 0.9096 |
| Memora P (policy) | **0.8740** | 0.8085 | 0.8598 | 0.6667 | 0.9251 |

Auxiliary metrics (S): BLEU 0.4693, F1 0.5562. (P): BLEU 0.4674, F1 0.5551.
S evaluation timestamp 2026-08-15T18:55Z; P 2026-08-15T22:59Z. Both runs completed
all 1,540 scored questions (10/10 conversations).

## Context vs the paper

The Memora paper (arXiv 2602.03315) reports 0.849 (S) / 0.863 (P) overall LLM-judge
on cats 1–4 with a **gpt-4o-mini** judge. This re-run uses a **gpt-4.1-mini** judge,
so the numbers are not a parity claim — they bracket the paper's published range
under a newer judge. These baselines exist for future same-harness comparison with
a Perseus Vault provider; they are NOT a head-to-head result.

## Evidence

Per-question output/eval JSONs are large and kept on the run host; sha256 recorded:

- `d2a01c639fa59dace384bd5536fe8ea4fb78f40eb41261077f6b2efcda423f8e`  memora_semantic_output.json
- `d87e572c6ecf73f951a657b788db73452da46f4c5edb51af6308312094eb999b`  memora_semantic_eval.json
- `397850db648992edcbeed967dce98d5115f4f5e2277dd22f2afcba90e0689008`  memora_prompt_output.json
- `226711f5603c07680847bbe87af94bdd10e2ae426152fbb73163266fb0f465d2`  memora_prompt_eval.json

Committed artifacts: `memora_s_scores.json`, `memora_p_scores.json`,
`memora_s_parameters.yaml`, `memora_p_parameters.yaml`.
