# Question-conditioned synthesis: parameterize the question, reflect at read time

Status: design specification
Date: 2026-07-21
Resolves: #741 · Vault-side mechanics for: Perseus-Computing-LLC/perseus#847
Related: `synthesis-hypothesis-lifecycle.md` (#739 — persistence rule for
reflective output), `abductive-graph-synthesis.md` (#740 — reflection
context this parameterizes), `memory-taxonomy-and-precedence.md` (insight
class), `served-memory-api.md` (briefing surface for reflective sections)

There is no purpose-free optimal summary: any "canonical" synthesis has
implicitly already picked a question. `mimir_dream` asks exactly one fixed
question — "what stable pattern do these collectively imply?" — which is
why its output resembles thematic compression. Given the same evidence,
"what did I learn this week?", "which assumptions turned out wrong?", and
"which explanation survived validation?" are different, legitimate
syntheses. This spec parameterizes the question and resolves the
stored-type-vs-read-time design tension in favor of a read-time reflective
operation over the bitemporal store.

## 1. Design tension resolved: a third operation, not a third store

The discussion posed the choice: reflective output as a **stored memory
type** (a third store beside episodic and semantic) or a **read-time
operation** over the canonical store. Resolution: **read-time operation.**

- Precomputing answers multiplies the synthesis problem by the number of
  questions, and the questions themselves drift as the agent's situation
  changes — a precomputed store is stale by construction.
- The bitemporal store already holds everything the answers are made of:
  `mimir_as_of`/`mimir_valid_at`/`mimir_bitemporal` track what was believed
  at T vs. now; `mimir_follow` holds the efficacy record; recall, `mimir_ask`
  and `mimir_global_recall` are primitive question-conditioned
  retrieval+generation.
- What gets persisted is not the answer but the **validated hypothesis**:
  a reflective output earns its place in the semantic layer only by
  surviving the prediction-testing lifecycle (see
  `synthesis-hypothesis-lifecycle.md`). The rest stays ephemeral.

## 2. `question` parameter on `mimir_dream`

`mimir_dream` accepts an optional `question` string. When present:

- **Cluster steering** — seeding stays similarity-based
  (`abductive-graph-synthesis.md` §1), but clusters are *ranked and
  filtered* by relevance to the question before the `max_clusters` budget
  is spent, so the LLM reflects over the clusters the question is about.
- **Prompt steering** — the asked question replaces the fixed default in
  the reflection prompt; the abductive output schema (`predicts`,
  `falsified_by`) still applies.
- **First-class metadata** — the question is recorded in the insight body
  as `question`, so persisted insights are attributable to the purpose
  that produced them.

`mimir_dream(question="what mistake kept repeating?")` must produce
insights mechanically distinct from a default run on the same corpus —
different cluster selection, different claims — not just a reworded
summary of the same clusters.

## 3. Reflective read-time queries: `mimir_reflect`

New read-only MCP tool answering structured reflective questions over the
bitemporal store at query time. Writes nothing.

```
mimir_reflect(question: string, workspace_hash?, scope?: {
                from_ms?, to_ms?, category?, topic_path?
              }) -> ReflectiveAnswer
```

```json
{
  "question": "how has my understanding of the gateway changed?",
  "lens": "bitemporal_diff",
  "answer": "…",
  "citations": [{"entity_id": "mem-…", "role": "evidence"}],
  "persisted": false
}
```

Two lenses, cheapest first:

| Lens | Mechanism | Handles | LLM? |
|---|---|---|---|
| `bitemporal_diff` | `as_of`/`valid_at`/`history` diff over the window | "how has my understanding changed?", "which assumptions turned out wrong?" (corrections + supersession trail) | No |
| `efficacy_record` | `mimir_follow` + validation-event tally query (lifecycle spec §2) | "which explanation survived validation?" | No |
| `synthesis` | Question-conditioned cluster + reflection (§2 pipeline, read-only) | "what did I learn this week?", "what mistake kept repeating, and why?" | Yes |

Lens selection is deterministic: questions matching diff/efficacy shapes
never invoke the LLM. Every answer carries cited entities; an answer
without citations is a bug.

## 4. Persistence rule

`mimir_reflect` itself never writes. Persistence happens only through the
lifecycle: when a reflective answer's claim is subsequently validated
(predictive stream, per `synthesis-hypothesis-lifecycle.md` §5), the caller
(or dream on a later pass) writes it as a semantic insight with
`derived_from` citations, `question` metadata, and
`lifecycle: hypothesis`. Reflective answers are working memory, not
knowledge, until reality votes.

## 5. Relationship to Perseus #847

Perseus issue #847 routes reflective queries at the orchestration layer
(query classifier: factual → lookup, semantic → recall, reflective →
synthesis). Vault's side of the contract is exactly §3: a single read-only
endpoint that (a) answers cheap-lens questions with no LLM call and (b)
answers synthesis-lens questions with cited evidence, guaranteed
write-free. Perseus's briefing surface maps to `served-memory-api.md`
views: a periodic "what changed in my understanding?" section is the
`bitemporal_diff` lens rendered as a served view.

## 6. Implementation slice

1. Add `question` param to `mimir_dream`: question-relevance cluster
   ranking + prompt substitution + `question` body field.
2. Add `mimir_reflect` with the three lenses; implement `bitemporal_diff`
   and `efficacy_record` first (pure queries over existing machinery), then
   the read-only synthesis lens reusing dream's cluster pipeline with the
   write step removed.
3. Wire persisted-reflective insights to carry `question` + `derived_from`
   and enter the lifecycle at `hypothesis`.
4. Hand perseus#847 the routing contract (§5).

No new storage: the reflective layer is an operation over the same
entities, links, history, and efficacy records recall already uses.

## 7. Acceptance checklist traceability

- #741: `question` parameter produces question-targeted insights
  mechanically distinct from a default run (§2) ✔; read-time reflective
  query returns cited answers without writing (§3) ✔; persisted
  reflective insights carry the question as first-class metadata (§2, §4)
  ✔; stored-type-vs-read-time tension resolved for a read-time operation
  with rationale (§1) ✔; perseus#847 vault-side surface defined (§5) ✔.
