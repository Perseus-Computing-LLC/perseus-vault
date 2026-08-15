# Attribution-Render Ladder (#1049)

**Status:** measured (2026-08-15) · **Scope:** how attribution presentation in
rendered recall payloads changes answer quality / refusal rate

## Measurement

Fixed store (12 facts, 4 outlets, deliberate outlet conflicts + version
sensitive facts), fixed queries (22: per-outlet, per-outlet-as-of, plain
controls, absent baselines), fixed grader (deterministic, shape-aware
resolver — offline, no model), real `perseus-vault` binary over MCP stdio.
Retrieval held constant (retrieval_ok_rate 1.0): the swing below is driven
purely by rendering. Harness: `benchmark/attribution_ladder/`.

| Render shape | Payload item | n | accuracy | refusal rate |
| --- | --- | --- | --- | --- |
| `bare` | fact text only | 22 | 0.273 | 0.727 |
| `key_only` | `[key] text` | 22 | 0.273 | 0.727 |
| `key_source` | `[key \| source] text` | 22 | 0.636 | 0.364 |
| `key_source_time` | `[key \| source \| date] text` | 22 | **0.818** | 0.182 |

**Reading.** Per-outlet questions cannot be answered from an unattributed
payload — the answerer refuses (honestly, avoidably). Adding `source` to the
header recovers them (+36 points); adding `date` recovers the as-of questions
(+18 points). The mechanism mirrors the Coalent measurement (0.641 → 0.678 →
0.731 on n=605) deterministically and offline.

## Decision (adopted from the data)

The **golden render shape is `key_source_time`**: `[key | source | date]`
attribution headers on every rendered recall payload item, with `?` for
missing provenance (never fabricated).

- The served context renderer (`context_render_schema`, #768) should carry
  these headers by default; the sweep is the regression harness for any
  future render-shape change (a change that lowers the ladder is rejected).
- Key-only and bare renders remain available as explicit opt-out modes for
  token-constrained surfaces; they are documented as answerability-reducing.

## Follow-ups

- Wire the golden shape into the served context renderer (implementation
  lands with the renderer; the measurement is the gate).
- Optional LLM-judged pass (`run.py --judge llm --limit N`) re-validates the
  deterministic ladder on a bounded spend when operator authorizes.
