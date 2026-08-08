# Deletion durability benchmark

Deterministic offline harness for the two Vault deletion policies:

- `logical_forget`: the canary must disappear from normal recall, reindex,
  background coherence, re-ingestion, and derived export while the untouched twin
  remains available;
- `permanent_purge`: the same checks must hold, followed by purge verification
  including archived lookup.

Run:

```bash
python3 benchmark/deletion/run.py --bin target/release/perseus-vault \
  --out /tmp/perseus-vault-deletion-report.json
```

The report contains only booleans, case/axis identifiers, counts, and a
signature. Raw canaries never enter the report. A nonzero exit is a blocking
failure; an unavailable binary or tool error is not converted to a pass.

This is a first executable vertical slice, not yet the complete deletion
protocol. Future cases must add history/journal scrubbing, vector/cache probes,
community/profile summaries, explicit share/federation propagation, and backup
restore checks.

Current checked run:

```text
18 / 18 checks passed
accuracy: 1.0
```

That result covers the implemented local protocol only; it is not a claim that
all possible external copies or backup substrates have been tested.
