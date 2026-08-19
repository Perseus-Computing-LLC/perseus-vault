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

## Official BEAM task lane

`beam_task/` is the provider-free contract and real-Vault retrieval lane for
the public [BEAM](https://github.com/mohammadtavakoli78/BEAM) workload. It is
separate from `beam/`, which remains the deterministic internal scale and
correctness gauntlet. The two suites must not be presented as the same dataset
or as interchangeable leaderboard evidence.

The lane reads the upstream layout directly:

```text
BEAM/
└── chats/
    └── 100K/<conversation-id>/
        ├── chat.json
        └── probing_questions/probing_questions.json
```

It also accepts `BEAM/chats` as `--data-root`. The selected source revision is
required to be a full 40-character commit ID; a floating branch or tag is
rejected. The manifest records the selected source-file digests, question
selection, retrieval mode, and answerer/judge identities.

### Provider-free contract path

The committed fixture has the same file layout and exercises the complete
hash-only report path without a Vault binary, network, model, or provider:

```bash
python3 benchmark/beam_task/runner.py \
  --data-root benchmark/beam_task/fixture \
  --size 100K \
  --source-revision aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --adapter fixture \
  --out-dir /tmp/perseus-beam-fixture
```

The CI contract tests run with:

```bash
python3 -m unittest benchmark.beam_task.test_protocol -v
```

### Real Vault retrieval run

Download or sparsely check out the BEAM repository separately, then run the
same task contract against a lean Vault binary. `fts5` is explicit here so the
run does not imply dense-embedding coverage:

```bash
python3 benchmark/beam_task/runner.py \
  --data-root /path/to/BEAM \
  --size 100K \
  --source-revision <40-character-BEAM-commit> \
  --adapter vault \
  --retrieval-mode fts5 \
  --bin target/release/perseus-vault \
  --out-dir /tmp/perseus-beam-100k
```

A run emits:

- `report.json` with one record per question and per-ability aggregates;
- `retrieval_replay.jsonl` with versioned, hash-only retrieval envelopes;
- `retrieval_snapshot.jsonl` with aligned synthetic hash-only snapshots for
  independent membership/order replay;
- manifest/config/result/custody SHA-256 digests;
- token-budget estimates and explicit answerer/judge model and prompt
  identities;
- retry/error telemetry without embedding provider messages in public output.

The report intentionally contains no raw BEAM questions, gold answers, chat
bodies, or retrieval bodies. Raw task data and runtime reports stay outside
the repository. The current lane is retrieval-only: answer generation and
rubric judging are represented as `not_measured` until a separately authorized
provider run supplies pinned model and prompt identities. Therefore this lane
makes no QA accuracy, competitor, or composite-score claim.

## Publication policy

`claim_register.json` is the claim boundary. A report is not externally
publishable merely because a command exits zero. Publication requires a frozen
control profile, dataset/manifest digest, binary digest, harness identity,
complete denominators, semantic signature, negative-claim section, and fresh
independent review.

The claim register explicitly records what the current local runs do **not**
measure: provider failure, external deletion propagation, backup/restore
semantics, downstream agent-task utility, and BEAM end-to-end answer/judge
accuracy.

## Reports

Raw run reports should be written to a temporary or explicitly selected output
directory. Do not commit runtime reports containing host-specific measurements
unless they are intentionally curated, fully fingerprinted, and reviewed.

See `package/README.md` for the common artifact/report contract.
