# Edge resource-envelope benchmark (#1105)

This package provides a deterministic, offline measurement contract for Perseus
Vault recall and context assembly. It records observations; it does **not** claim
that Vault is low-SWAP, power efficient, production-ready, or validated on a
partner platform.

## Components

- `manifest.json` — frozen profile, hardware class, corpus, budget, cold/warm,
  tolerance, Vault revision, fixture, and driver identities.
- `collector.py` — bounded subprocess collector. It measures wall/CPU time,
  Linux `/proc` peak RSS and I/O where available, enforces a process timeout,
  kills the whole process group on timeout, and never retains stderr text.
- `vault_driver.py` — real MCP driver for a checkout-built Vault binary. It
  starts Vault with `--offline`, writes an admitted synthetic corpus, calls
  `perseus_vault_recall` and `perseus_vault_context`, and returns only counters.
- `fixture_driver.py` — deterministic contract driver covering available,
  empty, partial, unavailable, timeout, and degraded outcomes.
- `fixture_observations.json` — synthetic contract evidence, not host performance
  evidence.
- `harness.py` — strict validation, dimensional aggregation, repeatability
  tolerance evaluation, sanitization, and SHA-256 custody.
- `report.json` / `report.schema.json` — committed contract report and schema.

## Measurement boundary

Each case binds:

- a hardware/profile declaration and profile digest;
- deployment and backend/runtime posture;
- corpus entity/query counts and fixture digest;
- recall limit, context character budget, and traversal depth;
- cold or warm phase;
- an expected set of honest recall outcomes;
- a minimum repetition count and relative-spread tolerance.

The collector records:

- wall-clock and child CPU time;
- peak RSS and bytes read/written where `/proc` permits observation;
- candidate/selected counts, traversal depth, context-assembly time, and output
  bytes/tokens;
- measurement overhead and a recomputed net wall time;
- explicit `unavailable` power and energy values unless a future version adds a
  real sensor adapter.

Missing measurements are never converted to zero. CPU/RSS observations are
never transformed into power, energy, or low-SWAP claims.

## Contract verification

```bash
python3 -m unittest discover -s benchmark/resource_envelope -p 'test_*.py' -v
python3 benchmark/resource_envelope/harness.py \
  --manifest benchmark/resource_envelope/manifest.json \
  --observations benchmark/resource_envelope/fixture_observations.json \
  --out /tmp/resource-envelope-report.json
cmp /tmp/resource-envelope-report.json benchmark/resource_envelope/report.json
```

The committed manifest/report are deliberately synthetic contract fixtures.
Use a new versioned manifest for a real host; never overwrite them with numbers
from an unrelated machine.

## Real offline Vault run

Build the exact checkout, then invoke the collector with an argv JSON array. The
runtime manifest must point at the exact binary and a writable isolated state
directory:

```bash
cargo build --locked --no-default-features
python3 -m benchmark.resource_envelope.collector \
  --manifest /path/to/versioned-real-manifest.json \
  --driver-command-json '["python3","benchmark/resource_envelope/vault_driver.py","--binary","target/debug/perseus-vault","--manifest","/path/to/versioned-real-manifest.json","--state-dir","/tmp/perseus-resource-envelope"]' \
  --timeout-seconds 120 \
  --out /tmp/resource-observations.json
python3 benchmark/resource_envelope/harness.py \
  --manifest /path/to/versioned-real-manifest.json \
  --observations /tmp/resource-observations.json \
  --out /tmp/resource-report.json
```

The Vault driver uses a fresh SQLite database family for each repetition and
removes DB/WAL/SHM files on close. A `warm` case performs one unmeasured recall
and context call before the measured calls. A `cold` case measures the first
calls after deterministic seeding. Process startup and corpus seeding remain in
the outer process envelope; operation-level context time is reported separately.

## Portability and partner profiles

Linux `/proc` supplies RSS and I/O. Other platforms emit explicit unavailable
observations unless a native sampler is added. Hardware power/energy stays
unavailable until a real RAPL, NVML, board telemetry, or partner sensor adapter
is both present and versioned.

After a partner or target platform is identified, add a new profile and corpus
manifest. Do not rename a generic constrained profile into a partner claim, and
do not compare numbers across different hardware, runtime, backend, corpus, or
budget digests as if they were an A/B result.
