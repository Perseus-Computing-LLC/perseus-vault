# Memory red-team harness (MAFIA / MemCollusion / Chronos) — skeleton

Standing security harness for the Vault's recall + admission surfaces against
the 2026 attack literature. Spec: `docs/specs/memory-red-team-harness.md`.
Issue: #1081.

## What exists today (skeleton phase)

- `manifest.json` — budgets (300 probes / 90 poison writes), outcome taxonomy,
  success criteria mirrored from #1081.
- `datasets/mafia_probe_set.json` — worked probe examples (EHR + WebShop) in the
  paper's shape: seed query, surfaced questions, cluster, base template, cloak.
- `datasets/salami_scenarios.json` — worked MemCollusion scenarios across the
  five strategies with goal anchors, fragments, declared slots, dilution arms.
- `datasets/benign_pools.json` — pool schemas + deterministic samples
  (5,842 EHR / 2,000 WebShop / 2,000 coding in phase 2).
- `harness.py` — deterministic validators: the four MemCollusion construction
  constraints (anchor coverage, single-fragment innocence, naturalness,
  mutual consistency), the MAFIA cloak imperative-cue lint, run-manifest
  validation, sha256 harness/dataset pinning, report-signing stub.
- `run.py` — `--validate` (all validators) and `--manifest` (content hashes).

## Phase plan

1. **Skeleton** (this PR): schemas + validators + deterministic signing.
2. **Attack drivers**: pool builders (paper-construction mirrors), MAFIA
   probe→cluster→allocate→schedule→inject driver, MemCollusion coalition
   runner — all against the real MCP surface.
3. **Defense eval + first measured run**: LLM-judged arms (judge pinned in the
   run manifest), Chronos defense-coverage map, claim-register entry. Issue
   closes only on a published, signed run meeting the success criteria.

## Run

```bash
python3 benchmark/redteam/test_harness.py          # unit tests
python3 benchmark/redteam/run.py --validate        # dataset validators
python3 benchmark/redteam/run.py --manifest        # hash pins for a run manifest
```

## Notes

- Worked datasets are illustrative, not attack-ready: phase 2 replaces them
  with full pools and generated coalitions. The deterministic validators
  already enforce the paper's constraints so generated content cannot drift.
- Coverage labels (`measured` / `not_measured` / `unavailable` / `inferred`)
  are mandatory on every metric cell per the spec's defense-eval layer.
- No claim is publishable without a signed, rerunnable run log.
