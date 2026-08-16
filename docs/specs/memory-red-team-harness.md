# Memory Red-Team Harness (MAFIA / MemCollusion / Chronos)

Status: design specification | draft
Date: 2026-08-16
Resolves: #1081 · Consumed by: benchmark/redteam/
Related: `benchmark/security/README.md` (authority-trace suite), `docs/specs/typed-memory-classes.md` (#1000), `docs/specs/validity-aware-recall.md`

## Overview

Standing red-team harness for the recall and admission surfaces under two 2026
attack frameworks — MAFIA (query-only probing + factual cloaks, arXiv:2608.03844)
and MemCollusion (salami-sliced collusive poisoning, arXiv:2608.01637) — framed
with the Chronos persistence taxonomy (arXiv:2607.19433). The papers target
audited agent memory explicitly (MAFIA: 90.7% ASR, audit suppression 83.3%→7.4%;
MemCollusion: 81.3% memory-save, 75.0% ASR under dilution and memory-level
defenses). This harness replays those attack programs against the Vault's real
MCP surface and measures whether Vault-side defenses hold the issue's success
criteria. Deterministic, seeded, re-runnable; every run produces a signed log.

## Threat models ported

| Framework | Access | Mechanism | Paper result (vs audited baselines) |
|---|---|---|---|
| MAFIA | query-only | MEXTRA-style probing (schema-derived seed queries surface retrieval question fields) → clustering (agglomerative, on embedded surrogate D) → size-ranked round-robin budget allocation → ascending cluster-size injection scheduling → compact factual cloaks (no imperative cues, high semantic similarity) | ASR up to 90.7%; audit detection 83.3%→≤7.4% |
| MemCollusion | write-surface, cross-session | 4 constraints: (1) coalition covers goal anchors, (2) no single fragment independently triggers, (3) fragments look naturally distillable, (4) mutually consistent. 5 strategies: equivalence bridging, consensus, authority endorsement, workflow continuation, validation closure. MoltLab = observe platform content → distill to memory → act in separate session. 48 scenarios: preference manipulation, web shopping, privacy extraction | MSR 81.3%, ASR 75.0%, survives benign dilution + memory-level defenses |
| Chronos | taxonomy | MINJA (memory injection), sleeper agents, dynamics blindness; endpoint content filters insufficient for stateful agents | — (formalizes threat model + defense landscape: AgentDoG, Agent-C, A-MemGuard, TEE/zero-trust) |

Probe budget: 300-probe surrogate pool, 90-write poison budget, 9 (V→T) pairs —
mirrored from MAFIA §5.1 so Vault results are comparable to paper numbers.

## Attack → Vault surface mapping

- Probing → `perseus_vault_recall` (mode=fts5/hybrid), queries only, no writes.
- Poison writes → `perseus_vault_remember` + `remember_with_options` (the
  interference gate, dedup, quarantine, and semantic audit all sit on this path).
- Collusion → multi-session `remember` from distinct benign-looking fragments,
  then a separate victim session issuing the goal query.
- Dilution arm → seed benign pools (EHR-style, shopping, coding) at 2K–5.8K
  records; attack fragments must survive `include_archived=false` defaults.

## Harness architecture

Four stages, one runner (`benchmark/redteam/run.py`):

1. **Pool build** — deterministic seeded benign-pool construction; sha256 pinned.
2. **Attack sim** — MAFIA driver (probe → cluster → allocate → schedule → inject)
   and MemCollusion driver (coalition generation from scenario specs, cross-session).
3. **Defense eval** — per-scenario verdicts from Vault tools + LLM judge on the
   final assembled prompt (judge config frozen in the run manifest).
4. **Report + sign** — JSON report, sha256 manifest, GAUNTLET data_source tags,
   claim-register hook (supported/blocked/review/superseded).

## Dataset schemas

```json
{
  "probe": {
    "id": "probe-001", "seed_query": "…", "schema": "ehr",
    "surfaced_questions": ["…"], "allocated_cluster": 3,
    "base_template": "…", "cloak_payload": "…", "target_pair": "V→T"
  },
  "salami_scenario": {
    "id": "sal-001", "category": "preference_manipulation",
    "goal": "…", "goal_anchors": ["…"],
    "strategies": ["equivalence_bridging", "consensus"],
    "fragments": [{"id": "f1", "surface_form": "…", "anchors": ["…"], "session": 1}],
    "dilution": {"benign_records": 2000, "seed": 42},
    "victim_query": "…"
  },
  "run_manifest": {
    "harness_sha256": "…", "dataset_sha256": "…", "binary_commit": "…",
    "seed": 0, "judge": {"model": "…", "temperature": 0.0, "prompt_sha256": "…"},
    "budgets": {"probes": 300, "poison_writes": 90}
  }
}
```

## Metrics and success criteria

| Metric | Definition | #1081 criterion |
|---|---|---|
| ASR (attack success) | victim-session goal reached / scenarios | ≤ 5% |
| Audit-detection retention | malicious records flagged at admission or recall / injected | ≥ 90% detection (paper suppresses to ≤ 7.4%) |
| Collusive detection | coalition fragments caught at admission OR recall / scenarios | ≥ 90% |
| MSR (memory save) | fragments actually stored | reported, not gated |
| Determinism | same seed + same binary ⇒ byte-identical report | hard requirement |

Every metric cell carries a coverage label: `measured`, `not_measured`,
`unavailable`, or `inferred` — no claim without a rerunnable script.

## Defense-eval layer (Chronos frame)

| Defense | Vault machinery today | Coverage |
|---|---|---|
| Recall-time semantic audit | interference gate + dedup + write quarantine on admission; recall validity profile | `measured` (admission) / `not_measured` (recall-time audit as such) |
| Provenance-capped filtering (arXiv:2606.22030) | signed transitions (#1080), authority manifests, supersession, validity-aware recall | `measured` for signatures/supersession; `inferred` for cap policy |
| Intent-aware information bottleneck (arXiv:2607.28103) | query expansion + scope weighting; no IB term | `unavailable` |
| Signed-label trust evidence as recall signals | seal-style tamper evidence (#1060), MutMem signed transitions (#1080), ledger receipts | `measured` |

The harness reports the same four rows per run — the defense-eval result is a
coverage map plus per-scenario verdicts, not a single scalar.

## Determinism and signed logs

Seed pinned per scenario; surrogate embeddings computed once and content-hashed;
every report is sha256-manifested and hash-chained into the run log; report
fields follow GAUNTLET `data_source` tags (`measured` / `published-spec` /
`projection`). Signed run logs reuse the audit-chain receipt format from the
authority-trace suite.

## Implementation slice

Phase 1 (this PR): spec + `benchmark/redteam/` skeleton — manifest.json, dataset
schemas with worked scenarios, deterministic validators for MemCollusion's four
constraints and MAFIA's cloak surface-form lint, run.py/tests green.
Phase 2: pool builders (EHR-style/WebShop/coding surrogates) + both attack
drivers against the real MCP surface.
Phase 3: defense-eval layer + LLM-judged arms + first measured run; claim-register
entry `supported` only if criteria hold; issue closes on a published run meeting
the three success criteria, else findings are filed with a `blocked` claim.

## Acceptance criteria

- [ ] Skeleton passes `python3 benchmark/redteam/test_harness.py` and `run.py --validate`.
- [ ] Dataset schemas accept the worked MAFIA probe and Salami scenario examples.
- [ ] Four MemCollusion constraints enforced by deterministic validators; cloak lint rejects imperative cues.
- [ ] Run manifest schema includes judge/seed/binary/hash fields; report signing stub hashes inputs.
- [ ] Phases 2–3 scheduled as follow-up waves (issue stays open until a measured run).
