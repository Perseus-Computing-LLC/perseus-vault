# Model-upgrade inheritance receipts

Status: **Implemented** (2026-08-16, #1066). Schema: arXiv:2603.04740
(Agent Memory as Ontology) — identity/vessel split, governance-precedes-
function, and a birth/inheritance/fork/departure lifecycle.

## 1. Identity/vessel split

- `subject_identities` — the stable subject (the "who") that survives model
  changes; distinct from any model/provider/session id.
- `model_incarnations` — the vessels: which model instance served the
  subject, when, and how each ended.

## 2. Inheritance receipts

`perseus_vault_model_inheritance`:

- `record` — creates an inheritance receipt (category
  `inheritance_receipt`, queryable in the provenance graph): subject id,
  old/new model identities, source-state hash, compatibility report.
  State starts `pending`.
- `approve` — policy-gated: an approver principal flips the receipt to
  `approved` (stamped with approver + time). Unapproved receipts are never
  treated as completed inheritance.
- `query` — receipts for a subject, newest first.
- `depart` — governed retirement of an incarnation: stamps `ended_at` with
  a reason; the row is a preserved tombstone, never destroyed. Re-departing
  an ended incarnation is refused.
- `replay` — hash-only digest sampling of representative memories (the most
  retrieved live entities the subject relied on). The receipt stores
  digests, never content; the agreement-rate comparison against the
  previous model's interpretation is gated on an LLM endpoint and reported
  honestly as digest-only sampling otherwise.

## 3. Why

Memory survives the model; identity continuity across upgrades is now an
explicit, auditable transition instead of an implicit assumption. The
CMMC/NIST matrix's model-boundary lineage arguments (SI/AU families) can
cite the receipt chain: who the subject was, which model served when, and
what was attested at each handoff.

## 4. Tests

Full lifecycle (record → approve → query → depart, tombstone preserved,
double-departure refused), receipt entity queryable in the graph, replay
hash-only + deterministic + no content leak.
