# Memory operations taxonomy: four questions, four contracts

Status: design specification
Date: 2026-07-21
Resolves: #745
Origin: design discussion with @sowerkoku on
sowerkoku/knowledge-kernel#2 (four-question framing, comment
5038446458; adopted with attribution, reply 5038886105)
Related: `synthesis-hypothesis-lifecycle.md` (#739 — consolidation's
output contract), `abductive-graph-synthesis.md` (#740),
`question-conditioned-synthesis.md` (#741 — reflection's output
contract), `memory-provenance-and-external-refs.md`,
`external-source-sync-contract.md` (#746 — the factual layer's sync
lifecycle). Orchestrator routing: Perseus-Computing-LLC/perseus#847
(`retrieval-orchestration-policy.md`, `reflective-queries.md`).

Memory-adjacent operations fail when they blur into one another:
recall pretending to be factual authority, consolidation pretending to
be reflection, reflection silently writing settled knowledge. This
spec names the four operations, the distinct question each answers,
and the output contract that keeps each in its lane. The operations
share one evidence base; the layering is what lets one store serve
all four questions without any operation becoming a second factual
authority.

## 1. The four operations

| Operation | Question | Mode | Output contract |
|---|---|---|---|
| Factual layer (kernel-style, external) | "What is true?" | synchronous lookup | Answers with provenance, or not at all |
| Episodic/semantic store (Vault recall) | "What happened?" | narrow, task-oriented, in-path | Episodes + memories, budget-clamped |
| Consolidation (`mimir_dream`, `mimir_consolidate`) | "What should remain?" | automatic, background, question-free | Writes durable semantic memory as *hypotheses* (#739) |
| Reflection (`reflect(topic)`, #741) | "What should be reconsidered?" | invoked, collaborative, question-driven | Writes nothing to semantic memory except through the stabilization gate |

The reasoning layer (LLM / orchestrator) asks the fifth question —
*"what follows?"* — and routes to whichever of the four can answer
(perseus#847). The fifth question consumes the other four; it is not
a fifth store.

## 2. Consolidation vs. reflection

The axis that separates the two write-side operations is **invocation
+ question**, not the machinery:

| | Consolidation | Reflection |
|---|---|---|
| Trigger | scheduled / automatic | invoked by user or agent |
| Human | none | collaborative participant |
| Question | question-free ("what stable pattern do these imply?") | question-driven ("what should I rethink?") |
| Goal | preserve knowledge | change understanding |
| Retrieval geometry | cluster + neighborhood (#740) | deliberately expansive (#741) |
| Cost posture | idle-time only | explicitly out-of-path; latency acceptable and disclosed |

A question-parameterized pass *with* a human invoker is reflection;
the same mechanism running unattended is consolidation. What Vault
calls dream **is** consolidation under this taxonomy — automatic,
background, question-free, preserve-oriented. Reflection is the
missing fourth operation; #741 pins its acceptance criteria.

Both stay out of the task-time retrieval path. "Automatic vs.
invoked" is the wrong axis for that constraint; **in-path vs.
out-of-path** is the right one, and both designs satisfy it.

## 3. Interpretations are overlays on facts, not edits to them

Reflection and consolidation change how existing knowledge is
*interpreted* — a different weighting of evidence, a revised
confidence, a reinterpretation of an existing fact — more often than
they introduce new facts. These decompose into existing machinery:

- **Re-weighting of evidence** → a certainty update on the overlay.
- **Reinterpretation of a fact** → a new version of the belief with
  the predecessor's valid-time bounded; the kernel-style fact
  underneath stays canonical and untouched.
- **Preference/philosophy shifts** → overlay writes in the belief
  layer (memory-taxonomy-and-precedence §5), never edits to the
  factual record.

This is the boundary that lets memory change its mind without the
factual layer ever noticing — and the reason derived interpretations
live in Vault rather than being pushed into any kernel-style store.

## 4. Validation: peer review is not replication

Consolidation and reflection both emit hypotheses (#739), and both
validation streams are required because they test different failure
axes:

- **Conversation / human scrutiny** (reflection's stabilizer) tests
  *coherence with lived experience*. It promotes a hypothesis and
  sets a strong certainty floor — but **promotion isn't tenure**. A
  human-validated hypothesis can still fail to generalize six months
  later.
- **Predictive validity** (the sparse, decisive stream) tests
  *generalization* against the environment, and keeps running after
  promotion.

Nothing gets to be unfalsifiable; some things get to be
well-reviewed. Human-validated insights remain in the lifecycle and
remain subject to the revision/split operator (#739 §4).

## 5. Episodic footprints of reflection

Reflection writes no semantic memory directly, but every session
leaves an **episodic footprint** — topic, challenged interpretation,
outcome (stabilized / inconclusive), participants. Rationale:

1. An inconclusive session is negative evidence; it prevents
   re-litigating the same question from zero.
2. Repeated inconclusive reflection on one topic is itself a
   finding: the question is malformed, or the evidence base is
   genuinely undecidable.

Minds changed or not, the attempt leaves a trace.

## 6. Invariants

- **I1 — Single factual authority.** Only the factual layer answers
  "what is true?", and only with provenance. No memory operation may
  serve a derived fact as verified ground truth (see
  `external-source-sync-contract.md` §Invariant).
- **I2 — Containment.** Provisional outputs (hypotheses, reflective
  products) never surface in narrow task-time recall until they pass
  the stabilization/validation gate.
- **I3 — Out-of-path write side.** Neither consolidation nor
  reflection taxes task-time retrieval latency.
- **I4 — No encroachment.** An operation answers its own question
  and no other; queries that need a different question are routed,
  not stretched (perseus#847).
