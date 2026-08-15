# Attribution Ladder (#1049)

Measures how **answer quality / refusal rate** changes with attribution
presentation in the rendered recall payload — same frozen store, same queries,
same grader; only the render shape varies.

Motivation: Coalent measured a ~9-point accuracy swing (0.641 → 0.678 → 0.731,
n=605) driven purely by how provenance is rendered — per-outlet questions
cannot be answered from an unattributed payload, so the answerer refuses
(honestly, but avoidably). This harness reproduces that mechanism **offline
and deterministically**: the grader is a shape-aware resolver that can only use
the fields a shape actually renders, so `bare` payloads cannot disambiguate
outlets and refuse per-outlet questions exactly like an LLM answerer would.

## Shapes (the ladder)

| Shape | Rendered payload item |
| --- | --- |
| `bare` | fact text only |
| `key_only` | `[key] text` |
| `key_source` | `[key \| source] text` |
| `key_source_time` | `[key \| source \| date] text` |

## Dataset

`dataset.json` — 12 facts across four outlets (Acme Blog, Nimbus Docs, Vertex
Forge, team retro) with deliberate outlet conflicts (ship date, budget,
ownership) and version-sensitive facts (postgres 14 → 15), plus 22 queries:

- `outlet` (8) — answerable only when the payload carries `source`
- `outlet_asof` (4) — answerable only when the payload carries `source` AND `date`
- `plain` (6) — answerable from text alone (control: all shapes must answer)
- `absent` (4) — nothing in the store can answer (refusal baseline)

## Running

```bash
# Deterministic judge (default; offline, free, reproducible)
python3 run.py

# Optional bounded LLM re-grade (spend-gated via --limit; needs OPENAI_API_KEY)
OPENAI_API_KEY=... python3 run.py --judge llm --limit 20

# Tests
python3 -m unittest test_harness
```

Drives the real `perseus-vault` binary over MCP stdio (`--bin` /
`PERSEUS_VAULT_BIN` to override), ingests the fixed store, recalls each query
(mode=fts5), renders the payload in all four shapes, and grades with the
resolver. Writes `out/report.json` (signed), `out/rows.jsonl`, and
`out/ladder.md`.

## Published ladder

See `out/ladder.md` after a run (and the decision record in
`docs/specs/attribution-render-ladder.md`).
