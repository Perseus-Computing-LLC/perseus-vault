# Mimir bi-temporal benchmark

A **reproducible, fully offline** check that Mimir's bi-temporal facts behave
correctly — that *time-travel returns the version that was actually live at a
past instant*, and that current recall never resurfaces a superseded one. It is
the temporal companion to the [`../recall/`](../recall/) recall-quality
benchmark.

> **Why this exists.** "Stop storing, start *maintaining*" is only a real claim
> if you can show it. When a fact is overwritten, Mimir keeps the prior version
> in history (`entity_history`) and can answer *"what did we believe at time
> T?"* via `mimir_as_of` — while normal recall stays live-only. This harness
> drives the **real shipped binary over MCP stdio** through fact updates and
> verifies exactly that, with a signed, re-runnable report anyone can reproduce
> with **no API key, no network, no LLM**.

## Run it

```bash
cargo build --release
python benchmark/temporal/run.py        # writes report.json, prints a summary
```

Exit code is non-zero if any check fails, so it can gate. `--dataset other.json`
plugs in a different scenario set; `--gap-ms N` widens the spacing between the
two writes.

## How it works

For each fact-update scenario it writes **v1**, marks a timestamp `t_mid`, writes
**v2** under the same `(category, key)` (which supersedes v1 into history), then
issues five checks against the real binary:

| Check | Asserts |
|---|---|
| `as_of_mid_returns_v1` | `mimir_as_of(t_mid)` returns the version live *between* the writes |
| `as_of_now_returns_v2` | `mimir_as_of(now)` returns the current version |
| `as_of_before_not_found` | `mimir_as_of(before-it-existed)` reports `found=false` |
| `recall_excludes_superseded_v1` | live `mimir_recall` never returns the superseded content |
| `recall_includes_live_v2` | live `mimir_recall` still returns the current content |

Wall-clock timestamps separate the two writes, so absolute times differ each run;
the **PASS/FAIL verdicts are deterministic**, and the `signature_sha256` is taken
over the verdicts (not the clock), so a correct implementation re-runs to an
identical signature. The committed [`report.json`](./report.json) is the
reference.

## The dataset

[`dataset.json`](./dataset.json) — `mimir-temporal-mini`: a handful of everyday
fact updates (a capital city, an employer, a default model, a commute). Each
carries a `v1_token`/`v2_token` (substrings unique to each version) and a
`probe` keyword for the recall checks. Add entries of the same shape to extend
coverage; the harness is dataset-agnostic.

## Results (committed [`report.json`](./report.json))

`mimir-temporal-mini`: **20/20 checks, 100% accuracy** — `as_of` returns the
correct version at every probed instant, and recall is live-only across all
scenarios. *(Measured on the bundled MSVC build, Windows 11; the methodology and
verdicts are the point and are platform-independent.)*

## Three-axis gauntlet (full SQL:2011 bi-temporal)

Where `run.py` above proves the **transaction-time** axis with fact-overwrite
scenarios, [`gauntlet.py`](./gauntlet.py) is the full **three-axis** suite (#553).
It drives the real binary over MCP stdio through the hard bi-temporal cases that
single-axis competitors get wrong:

| Axis | MCP tool | Question it answers |
|---|---|---|
| transaction-time | `mimir_as_of` | "what did we **believe** at tx T" |
| valid/application-time | `mimir_valid_at` | "what was **true** in the world at valid T, per current knowledge" |
| full bi-temporal | `mimir_bitemporal` | "as of belief at tx_at, what was true at valid_at" |

Scenarios: retroactive correction, proactive/future-dated facts,
belief-vs-truth divergence, **out-of-order arrival** (a later-recorded fact with
an *earlier* valid period must still stitch the timeline correctly), and closed
periods.

Run it (one command, fully offline — no network, no API key, no LLM):

```bash
cargo build --release
python benchmark/temporal/gauntlet.py --bin target/release/perseus-vault
```

### Results (committed [`gauntlet_report.json`](./gauntlet_report.json))

`perseus-vault-bitemporal-gauntlet`: **13/13 checks, 100% accuracy** across all
three axes (valid-time 10/10, transaction-time 1/1, bi-temporal 2/2) — including
out-of-order arrival, which naive valid-time stores mis-order. Day offsets are
resolved against a runtime anchor so absolute times vary run-to-run, but the
PASS/FAIL verdicts and their `signature_sha256` are deterministic for a correct
implementation. CI gates on this via [`gate.py`](./gate.py).
