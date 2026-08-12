# Utility-driven lifecycle promotion (#1001)

Status: normative. Surface: `entities.utility_score`, journaled
`auto_promotion` events, `next_epistemic_state` transition function.

Borrowed from CogniCore's signal design (retrieval-count → ACTIVE,
outcome-feedback → VERIFIED) — NOT its thin enforcement. The ladder maps
onto the vault's #880 epistemic states:

| CogniCore rung | Vault representation | transitioned by |
|---|---|---|
| CANDIDATE | epistemic_state `candidate` | write default |
| ACTIVE | status `active` | write default (not a stored rung) |
| VERIFIED | epistemic_state `verified` | #1001 auto-promotion or explicit operator verification |
| ARCHIVED | `archived = 1` | existing decay machinery (unchanged) |

## Accrual (saturating, cap 100.0)

| signal | delta | hook |
|---|---|---|
| reinforced recall hit | +1.0 | `apply_recall_side_effects` |
| citation (`derived_from` / mark_useful) | +5.0 | `mark_useful[_by_id]` |

**#247 preserved**: utility accrues ONLY on side-effect-bearing
interactions. A default recall is a pure read and never mutates — the
frozen-recall determinism contract is unchanged.

## The transition function

`next_epistemic_state(state, utility, outcomes)` is pure and total:

- `candidate` → `verified` iff utility >= 10.0 AND usefulness_count >= 1
- all other states → no movement (never auto-demote; corroboration always
  requires independent-source evidence per #880; unknown states never move)

## Application

- Runs over EXACTLY the ids touched by the accruing hook (never an
  unbounded sweep), after the hook's own UPDATE.
- CAS-guarded: `UPDATE ... SET epistemic_state = ?1 WHERE id = ?2 AND
  epistemic_state = ?3` — a concurrent explicit operator promotion wins;
  auto-promotion never fights it.
- Every transition journals `event_type = 'auto_promotion'` carrying
  from/to state, utility snapshot, and outcome count — the journal is the
  evidence surface; there is no silent mutation.

## Operator override

Explicit verification/demotion tooling (#880) is unchanged and always
outranks auto-promotion (the CAS above guarantees it).
