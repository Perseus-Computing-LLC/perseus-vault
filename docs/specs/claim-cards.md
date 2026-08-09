# Evidence-backed claim cards (#852)

Status: implemented (branch `feat/vault-852-claim-cards`)
Surface: `perseus_vault_claim_card` (MCP, read-only)

## What a claim card is

A deterministic, versioned projection of one entity into a compact,
reviewable object: the claim, its provenance class, valid-time vs
recorded-time, confidence/uncertainty and support, evidence references,
supersession/contradiction/stale state, a sanitized `agent_projection`
hash-bound to the selected evidence and policy, and machine-readable
reason codes explaining why the projection is serveable, constrained, or
withheld.

It is a **view over existing entities and links — never a second source of
truth**. Nothing is written by building a card; `confirm`/`correct`/
`exclude`/`revalidate` map to the existing governed mutation tools
(`perseus_vault_score`, `perseus_vault_follow`, `perseus_vault_correct`, `perseus_vault_supersede`,
`perseus_vault_forget`), all of which create history rather than editing evidence
in place. The card's `lifecycle` block states that mapping per state.

## Provenance class (derived, never guessed)

Roll-up per `provenance-classes-derived-facts.md` §1 from
`origin.memory_kind` + presence of evidence links
(`evidence_for` / `derived_from` / `promoted_to`):

| class | rule |
|---|---|
| `source_human` | `memory_kind` = asserted, imported |
| `fact_extracted` | `memory_kind` = extracted, observed |
| `fact_derived` | `memory_kind` = inferred **and** evidence links present |
| `inference_agent` | `memory_kind` = inferred, no evidence links |
| `null` | undeterminable — never guessed |

A derived fact without evidence links is `inference_agent` regardless of
author intent (spec rule: the evidence set defines the boundary).

## State flags

- `superseded` — status `deprecated` or `superseded_by` set;
  `superseded_by` / `supersedes` carry the ids.
- `stale` — decay < 0.5, or `valid_to` / `invalidated_at` in the past.
- `contradicted` — any live entity tagged `contradiction` links to the
  claim, or the claim itself carries the tag.
- `quarantined` — status `quarantined`.
- `revalidation_required` — last touch older than 90 days and not
  superseded.
- `archived` — soft-deleted row; the card withholds the projection.

## Serveability & reason codes

`reason_codes[0]` is `serveable` or a withhold reason; further codes are
flags (`stale`, `stale_evidence`, `missing_provenance`, `contradicted`,
`superseded`, `revalidation_required`). Withhold rules (mirror #684):

| reason | rule |
|---|---|
| `archived` | entity is archived |
| `revoked_access` | visibility private/fleet and caller `agent_id` ≠ author |
| `scope_mismatch` | non-global workspace entity and caller `workspace_hash` differs |

A withheld card still returns structural metadata (ids, times, state,
reason codes); only the evidence refs and the agent projection are
suppressed. Evidence refs are **metadata only** (entity id, claim text,
relationship, class, confidence, stale) — raw bodies, secrets, prompts,
and inaccessible sources never cross by default (`agent_projection.excluded`
names what was withheld).

## Determinism contract

- `digest` = sha256 over the **canonical** (recursively key-sorted)
  serialization of a fixed subset: claim, class, times, confidence,
  verified, epistemic state, support count, state flags, evidence
  (id + relationship), lifecycle text, and the card version.
- Evidence refs are sorted by entity id; supporter discovery is
  order-independent; JSON key order in source bodies is irrelevant.
- `agent_projection.digest` = sha256(projection text + "|" + card digest),
  binding the projection to the exact evidence set and policy the card
  was computed under.
- Bumping `CLAIM_CARD_VERSION` freezes the digest input set; a schema
  change bumps the version rather than silently redefining digests.

## Cost

One O(live entities) scan per card (the `links` column is a JSON text
array with no index), the same cost class as the beliefs overlay. An
indexed link lookup is a possible follow-up; cards are read-only and
cache-friendly at the caller.

## Tests

Nine cases in `src/claim_card.rs`:
clean evidence (+ digest determinism across link order and key order),
one-off inference (ungrounded flag), contradictory support, stale
evidence, supersession, scope mismatch, revoked access (intruder vs
owner), bi-temporal + archived, and include_* flag honoring.

## Non-goals (from #852, honored)

No claim that confidence is calibrated; no universal factuality or safety
guarantee; no parallel memory store.
