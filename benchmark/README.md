# Perseus Vault benchmark portfolio

This directory contains orthogonal benchmark suites for the Vault memory
substrate. It intentionally does not publish a composite memory score.

## Fast local verification

```bash
cargo build --release
python3 benchmark/run_smoke.py --bin target/release/perseus-vault
```

The smoke runner executes the bounded real-binary suites:

- `quality/` — contract and safety gate;
- `correction/` — contradiction and supersession durability;
- `deletion/` — local forget and purge durability;
- `freshness/` — healthy write-to-readable and restart behavior.

Economics helpers are tested independently under `economics/`. The existing
`recall/`, `temporal/`, `context_selection/`, and scale documentation remain
available as specialized or historical benchmark surfaces.

## Publication policy

`claim_register.json` is the claim boundary. A report is not externally
publishable merely because a command exits zero. Publication requires a frozen
control profile, dataset/manifest digest, binary digest, harness identity,
complete denominators, semantic signature, negative-claim section, and fresh
independent review.

The claim register explicitly records what the current local runs do **not**
measure: provider failure, external deletion propagation, backup/restore
semantics, and downstream agent-task utility.

## Reports

Raw run reports should be written to a temporary or explicitly selected output
directory. Do not commit runtime reports containing host-specific measurements
unless they are intentionally curated, fully fingerprinted, and reviewed.

See `package/README.md` for the common artifact/report contract.
