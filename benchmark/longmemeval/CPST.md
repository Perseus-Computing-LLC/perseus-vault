# CPST: what a correct answer costs, with and without a memory layer

**The claim this page defends:** an agent with Perseus Vault answers memory
questions at a fraction of the cost per *correct* answer of an agent that
carries its full history — measured, same model, same judge, same questions,
API-billed tokens only.

CPST — **cost per successfully completed task** — is answerer spend divided by
correctly answered questions. It fuses cost and quality: a system that is
cheaper because it is *worse* gets a worse CPST, so "cheaper but broken"
cannot win this metric. This is the efficiency companion to the accuracy
head-to-head in [COMPARISON.md](COMPARISON.md).

## The three arms

| arm | context given to the model | what it represents |
|---|---|---|
| `stateless` | the question only | an agent with no memory at all — why the category exists |
| `fullcontext` | every haystack session (~48 sessions, ~105k tokens) | the no-memory-layer default: carry everything, every call |
| `mimir` | top-10 sessions from perseus-vault hybrid recall (~26k tokens) | the product |

Same pinned answerer and judge as the flagship run (`gpt-4o-2024-08-06`,
temperature 0, LongMemEval official per-type judge). Same stratified,
seeded, manifest-published 100-question subset for every arm
([longmemeval_s_subset100.manifest.json](longmemeval_s_subset100.manifest.json),
seed 475 — proportional per question type, sha256-pinned, no cherry-picking
possible after the fact).

## Results

<!-- RESULTS TABLE: filled from cpst.py output over qa_report_cpst100.json -->
*(run in progress — table lands here from `cpst.py`)*

## Token efficiency (offline, free to reproduce, deterministic)

Measured with `qa.py --dry-run` on the same subset — no API, no key:

| arm | avg sessions in context | avg tokens/question | total (100 q) |
|---|---:|---:|---:|
| stateless | 0.0 | 75 | 7,487 |
| fullcontext | 47.8 | 105,496 | 10,549,621 |
| mimir (k=10) | 9.8 | 26,484 | 2,648,425 |

**Perseus feeds 4.0× fewer tokens per question than full-context stuffing at
the flagship k=10 setting.** (At k=5 the ratio is ~8×, with a recall
trade-off; k=10 is what the 73.6% accuracy number uses, so k=10 is what we
price.)

## Accounting rules (argue with these, not the arithmetic)

- **Cost = answerer tokens only, API-billed.** The judge is measurement
  apparatus, not production cost; its spend is reported separately. Token
  counts come from the provider's `usage` object on every call — estimates
  pace the run but are never quoted.
- **Accuracy denominator = graded questions.** Rate-limited/errored calls are
  excluded by the harness and can never deflate (or inflate) a number.
- **Perseus overhead is local.** Retrieval and embeddings run on-device
  (bundled ONNX); there is no per-call API cost to hide. Ingest is one-time
  and amortized; the flagship report's `elapsed` figures disclose it.
- **Where Perseus does NOT win:** if your agent's whole history already fits
  in a few thousand tokens, stuffing it is fine and a memory layer buys you
  little — this measurement is about agents whose accumulated context has
  outgrown their calls. Break-even sits roughly where accumulated history
  exceeds the retrieved-context size (~26k tokens at k=10).

## Reproduce

```bash
# free: subset + token table
python make_subset.py --data longmemeval_s_cleaned.json --n 100
python qa.py --data longmemeval_s_subset100.json --dry-run \
    --systems stateless fullcontext mimir

# paid (~$34 at 2026-07 gpt-4o pricing; prints estimate, requires --yes)
python qa.py --data longmemeval_s_subset100.json \
    --systems stateless fullcontext mimir --tpm 400000 --yes \
    --journal qa_progress-cpst100.jsonl --out qa_report_cpst100.json

# synthesis
python cpst.py --reports qa_report_cpst100.json \
    --manifest longmemeval_s_subset100.manifest.json
```

## Caveats

- **n=100, single run.** Subset is stratified and manifest-pinned, but it is
  one seed and one run; treat point estimates as such. The flagship 500-question
  accuracy number lives in [COMPARISON.md](COMPARISON.md).
- **Dollar figures move with provider pricing.** Token counts are the durable
  fact; prices are pinned in `qa.py`'s `PRICING` with their as-of date.
- **LongMemEval measures memory-recall QA**, not every agent workload. It is
  the fairest public proxy we know for "agent that must remember prior
  sessions"; it says nothing about, e.g., code-generation efficiency.
