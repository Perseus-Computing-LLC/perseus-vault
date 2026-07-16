# BEAM — bi-temporal correctness + determinism at scale (#685)

Named for **BEAM** (Beyond a Million Tokens, arXiv:2510.27246), which tests at
128K / 500K / 1M / 10M tokens to defeat the "dump everything in context" cheat.
Our claim is orthogonal to a context-window benchmark: **FTS5 + deterministic
bi-temporal retrieval must not degrade as the corpus grows.**

BEAM embeds the CI-verified bi-temporal gauntlet
([`benchmark/temporal/gauntlet.py`](../temporal/gauntlet.py) — 13 checks across
the three SQL:2011 axes) inside a filler corpus sized to each token tier and
asserts, at every tier:

| Property | How |
|---|---|
| **Correctness holds** | the embedded gauntlet still scores 13/13 (100%) |
| **Determinism** | two independent runs produce the identical signature over PASS/FAIL verdicts |
| **Latency stays flat** | per-axis point-lookup p50/p95 at scale (point lookups are `(category,key)`-indexed, so they should stay near-flat) |

The harness **reuses `gauntlet.run_scenarios` / `build_report` verbatim** — one
source of temporal truth, so BEAM can only ever agree with the gauntlet's own
verdicts. The filler corpus is bi-temporally inert (valid-since-creation, never
superseded, its own category), so it enlarges the search space without
perturbing the scenarios.

## Running

```bash
cargo build --release            # or --no-default-features for the lean binary
                                 # (FTS5 + bi-temporal work without embeddings)

# small tier, fast, local:
python benchmark/beam/run.py --tiers 128K --out /tmp/beam.json
python benchmark/beam/gate.py /tmp/beam.json

# full ladder — 1M/10M are heavy (minutes to populate, GB of DB). Run on the
# GPU/large-mem fleet; see benchmark/lambda/ for orchestration:
python benchmark/beam/run.py --tiers 128K 500K 1M 10M --out /tmp/beam.json
```

Validate the harness logic with **no binary** (CI-cheap; also runnable where no
build exists):

```bash
python benchmark/beam/run.py --self-test
```

## Tiers & sizing

Token budgets are approximate corpus sizing (`~4 chars/token`); the number that
matters and is reported exactly is the **filler entity count**. `--self-test`
prints the entity counts per tier.

| Tier | Token budget | Notes |
|---|---|---|
| 128K | 128,000 | fast, local, CI-gateable |
| 500K | 500,000 | local on a dev box |
| 1M | 1,000,000 | manual / nightly (large DB) |
| 10M | 10,000,000 | fleet only (`benchmark/lambda/`) |

## Status & reproducibility

The harness and its gate are committed here. Published tier numbers come from a
run on named hardware and are captured in `report.json` (content-hashed per
tier via the gauntlet sha256 fingerprint), **not hand-written**. The 1M/10M tiers are executed on the
benchmark fleet (they need real memory + minutes of population); their
`report.json` is committed as the reference once produced. `--self-test` and the
128K tier run anywhere.

Fully offline: no network, no API key, no LLM.
