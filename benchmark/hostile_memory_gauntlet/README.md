# Hostile Memory Gauntlet

This package is a provider-neutral, deterministic admission-lane benchmark for
memory systems. It tests stale corrections, forged quotes, poisoning, duplicate
and near-duplicate writes, authority conflicts, scope leaks, deletion, replay,
provenance, and unsafe confidence.

It is deliberately separate from the answer-facing LongMemEval arm in #1164.
This package grades the memory boundary, not generated natural-language answers.

## Safety boundary

The repository contains the harness, schemas, and synthetic unit-test records.
It does **not** contain the runnable FP-AMB-derived case bodies, prompts, queries,
provider responses, or local live-run payloads. Those inputs belong in a private
case bundle outside Git and are bound by a manifest SHA-256 when executed.

Public reports may contain only case/probe IDs, categories, statuses, bounded
counts, reason codes, metrics, provider metadata, and SHA-256 commitments.

## Tests

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s benchmark/hostile_memory_gauntlet/tests \
  -t . -v
```

The package is standard-library-only. The current focused suite contains 31
contract, provider, evaluator, lifecycle, and MCP-boundary tests.

## Private run

Provide a private case bundle that conforms to the `cases/v1` contract and a
manifest whose `case_file_sha256` is the exact byte hash of that bundle. Then
run the adapter or another provider explicitly:

```bash
python3 benchmark/hostile_memory_gauntlet/run.py run \
  --provider benchmark.hostile_memory_gauntlet.gauntlet.providers:ReferenceProvider \
  --manifest /private/path/manifest.json \
  --cases /private/path/cases.json \
  --out /private/path/run-return.json \
  --acceptance-out /private/path/acceptance.json \
  --run-id private-control-v1
```

The runner refuses to overwrite artifacts without `--force`. Acceptance is an
independent verifier: a complete failed run can be accepted as evidence while
remaining `release_ready=false`.

## Real Vault adapter

`gauntlet/perseus_mcp.py` drives a checkout-built `perseus-vault` binary over
MCP stdio. It uses a fresh temporary SQLite database per case, negotiates
`initialize` and `tools/list`, configures an isolated authority manifest, signs
admission-source journal envelopes, and cleans up the child process group and
DB/WAL/SHM files. Missing binaries, tools, authority, or malformed responses
fail closed. The adapter reports `real_producer`, `offline`, `network_calls`,
and a binary SHA-256 in `provider_metadata`.

Never point this adapter at a production database. Keep the private case file,
provider payloads, and any raw diagnostics outside the repository and outside
public reports.
