# Perseus Vault recall-quality benchmark

A **reproducible, fully offline** measurement of whether Perseus Vault retrieves the
*right* memory — recall@k and MRR — across its three search modes. This is a
**quality** benchmark; the latency/throughput suite lives in
[`../run.py`](../run.py).

> **Why this exists.** The agent-memory field's recall numbers are notoriously
> unreproducible (the same system has been reported at wildly different LOCOMO
> scores across sources). Perseus Vault's pitch is local-first/offline, so its
> credibility benchmark should be one anyone can re-run on their own machine
> with **no API key, no network, no LLM** — and get the same number. That is
> what this harness is.

## Run it

```bash
cargo build --release            # builds perseus_vault with bundled embeddings (default)
python benchmark/recall/run.py   # auto-locates target/release/perseus-vault
```

Or point at a binary explicitly:

```bash
python benchmark/recall/run.py --bin /path/to/perseus-vault
PERSEUS_VAULT_BIN=/path/to/perseus-vault python benchmark/recall/run.py
```

It writes [`report.json`](./report.json) and prints a summary. Exit code is 0
on success.

## How it works

1. Ingest the dataset's memories via `perseus_vault_remember`.
2. Populate dense vectors with the **bundled** ONNX model via `perseus_vault_embed`
   (local, no network, no API key).
3. For each query, call `perseus_vault_recall` in each mode (`fts5`, `dense`, `hybrid`)
   and score recall@k / reciprocal rank against the query's known-relevant keys.

Everything runs against the **real shipped binary over MCP stdio** — the same
path a production agent uses — so the numbers reflect what users actually get.

## The dataset

[`dataset.json`](./dataset.json) — `perseus_vault-recall-mini`, a 24-memory /
24-query personal-assistant set in the LOCOMO / LongMemEval mold. It is
deliberately **paraphrase-heavy**: each query is worded differently from the
memory that answers it (e.g. *"does the user own any animals"* → *"I have a
golden retriever named Max"*), so keyword-only search is stressed and semantic
retrieval is rewarded. Domain-adjacent distractors are included.

It is intentionally small and self-contained so the benchmark needs no download.
To run the **full** public benchmarks, pass a dataset of the same shape built
from LOCOMO or LongMemEval — the harness is dataset-agnostic:

```bash
python benchmark/recall/run.py --dataset locomo_subset.json
```

```json
{"memories": [{"category": "...", "key": "...", "note": "..."}],
 "queries":  [{"q": "...", "relevant": ["key1"]}]}
```

## Results (this dataset, committed [`report.json`](./report.json))

| Mode | recall@1 | recall@3 | recall@5 | MRR |
|---|---|---|---|---|
| `fts5` (keyword) | 4.2% | 12.5% | 20.8% | 0.131 |
| `dense` (bundled embeddings) | **91.7%** | **95.8%** | **100%** | **0.948** |
| `hybrid` (RRF) | 87.5% | 95.8% | 95.8% | 0.917 |

*Measured on `perseus-vault.exe`, Windows 11, bundled int8 all-MiniLM-L6-v2. Your
absolute numbers may differ slightly by platform/binary; the methodology and the
relative picture are the point.*

### Honest findings

- **Bundled local embeddings carry recall.** On paraphrased queries, keyword
  search alone is near-useless (4.2% recall@1) — it cannot match *"own any
  animals"* to *"golden retriever"*. The offline dense model gets it right 92% of
  the time at rank 1 and **100% within the top 5**, with **zero network calls**.
  That is the local-first promise made measurable.
- **This set is adversarial to keyword search by design.** A real corpus has
  some lexically-overlapping queries where `fts5` does fine; don't read 4.2% as
  Perseus Vault's keyword quality in general — read it as "paraphrase needs semantics."
- **`hybrid` now tracks `dense` on this set, after #247.** This benchmark
  originally surfaced two real RRF bugs: hybrid recall@1 collapsed to ~21%
  (versus dense's 92%), and hybrid drifted ~1–2 queries run-to-run. Both are
  fixed:
  - *Dilution.* The keyword arm previously matched natural-language queries on
    stopwords ("the", "have", "does"), returned the whole corpus ordered by
    *popularity*, and rank-based RRF gave that noise full weight — burying the
    dense rank-1 hit. The hybrid keyword arm now drops stopwords, ranks by **BM25
    relevance** instead of popularity, is **dropped entirely when it finds no
    content match**, and is fused at a reduced (dense-primary) weight. Hybrid
    recall@1 rises from ~21% to **87.5%**. The residual gap to pure dense is a
    handful of queries where a lexical match is genuinely misleading (e.g.
    *"foreign language"* matching the *programming-language* memory) — an
    inherent fusion trade-off, not the old pathology.
  - *Non-determinism.* RRF now breaks score ties by entity id (instead of falling
    back to randomly-seeded hash-map iteration order), and the keyword arm is a
    **read-only, BM25-ranked** sub-query that no longer depends on wall-clock
    decay or `perseus_vault_recall`'s access side-effects. **All three modes are now
    byte-stable run-to-run**, so the pinned `signature_sha256` covers `hybrid`
    too.

## Reproducibility

All three modes (`fts5`, `dense`, `hybrid`) are deterministic for a given
dataset + binary + platform; re-running yields an identical `signature_sha256`.
Exact dense rankings can vary marginally across CPU architectures (ONNX
floating-point), so treat the committed `report.json` as the reference for *this*
platform; CI (Linux) is the canonical re-run when wired up.

## Prospective query hints (#919)

`--hints` ingests `memories[].hints` (server runs with
`PERSEUS_VAULT_HINTS_ENABLED=1`) and records `hints_enabled` in the report
and signature. `dataset_hints.json` is a vocabulary-gap set whose query
tokens appear only in the relevant entity's hints; the fts5 arm is the
measure. Reference delta on this dataset (Linux, bundled ONNX):

| run | fts5 R@1 | fts5 MRR |
|-----|----------|----------|
| hints off | 0.0 | 0.000 |
| hints on  | 100.0 | 1.000 |

Run both variants (`--hints` and without) before any default-on flip; dense
arm values are reference-only (embed step is per-process and noisy).
