# Feature Spec: Keystone-suggestion instruction extraction (#889)

**Status:** implemented (schema v32)
**Depends on:** keystones (#683), trust tiers (#684), operator review surface
**Competitive driver:** Mnemosyne MEMORIA instruction extraction; bug #507 lesson
(word-boundary anchoring)

## Problem

Extracting imperative instructions ("never X", "whenever X") from user text is
a proven pattern, but unanchored matching inverted meaning in production:
"whenever" contains "never", so a bare `never` pattern matched inside
"whenever needed we can use it" and stored the opposite instruction ("never
needed we can use it"). The fix is word-boundary anchoring per locale.

The Vault's keystones are authored policy rules. Auto-suggesting candidates
from `correct` captures reduces authoring friction while keeping the
governance gate: extraction never writes policy.

## Design

### Extraction (src/instruction_extraction.rs)

- Manual word-boundary matching (no regex dependency): a trigger matches only
  when both neighbors are non-word chars (Unicode alphanumeric), on both sides.
- Locale trigger tables: en (never/always/whenever/do not/must not/must/only/
  unless/should/after/before), de (nie/immer/sobald/wenn/darf nicht/muss),
  ru (никогда/всегда/когда/нельзя/обязательно), it (mai/sempre/quando/non
  devi/devi), es (nunca/siempre/cuando/no debes/debes).
- Unicode-aware case folding (ASCII folding alone misses Cyrillic В→в).
- The instruction is the containing sentence/clause, capped at 240 chars;
  8 suggestions max per text.

### Suggestion queue (schema v32, `keystone_suggestions` table)

| Column | Meaning |
|---|---|
| `id` | `ksug-<uuid12>` |
| `source_entity_id` | the correction entity id (citation) |
| `source_category` | `correction` |
| `instruction` | extracted directive sentence |
| `pattern_locale` / `matched_pattern` | provenance of the match |
| `status` | `pending` → `approved` \| `rejected` (one-way) |
| `decided_at_unix_ms` / `decided_by` | decision audit |
| `workspace_hash` | workspace scope |

Dedupe: an instruction already pending (from any source) is never re-queued;
`UNIQUE(source_entity_id, instruction)` guards the table.

### Hook

`perseus_vault_correct` scans `user_correction`, `wrong_approach`, and
`task_context` after the capture succeeds and queues candidates. The response
gains `keystone_suggestions` (id/instruction/locale/pattern) and
`keystone_suggestions_count`. Extraction is fail-open: a broken extractor can
never fail a correction; errors (if any) surface as `extraction_errors`.

### Tools

- `perseus_vault_keystone_suggestions(status?, workspace_hash?, limit?)` —
  read-only queue listing with source citations.
- `perseus_vault_keystone_suggestion_decide(id, action, scope?, scope_id?,
  weight?, trust_tier_required?, author_trust_tier?, agent_id?,
  workspace_hash?)` — the ONLY promotion path:
  - `approve`: re-runs the #683/#684 trust-tier gate (registry-backed tier
    wins over caller assertion), then `keystone_set` with the extracted
    instruction, then marks the suggestion `approved`.
  - `reject`: marks `rejected`, writes nothing.

### Operator review surface

`perseus_vault_operator_review` includes `keystone_suggestions` (pending
candidates, newest first) so decisions happen in the existing review queue.

## Acceptance (verified in tests)

1. Anchored extraction: the whenever/never inversion regression — "whenever
   needed we can use it" yields only the `whenever` pattern, never a "never"
   instruction; and `never` does not fire inside `whenever` or vice versa.
2. Suggestions only: `correct` never creates keystones; promotion requires an
   explicit `approve` decision.
3. Trust gate on promotion: a registry-enforced tier-1 author is denied;
   tier-2 succeeds with `trust_enforced: true`.
4. Decisions are one-way: an already-decided suggestion is immutable.
5. Queue surfaced in the operator review surface with source citation.
6. Dedupe: repeated corrections carrying the same pending directive queue one
   suggestion.
7. Locale coverage: de/ru/it/es triggers match with boundaries; false friends
   ("quando" vs "cuando") do not cross locales.

## Out of scope

- Extraction from arbitrary session text (issue lists it as optional; the
  correct-capture hook covers the primary path).
- Automatic promotion — governance is deliberately manual.
