# Source anchors, correction workflows, and long-horizon retention

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #721 (parent), #725
Related: `memory-taxonomy-and-precedence.md`, `memory-provenance-and-external-refs.md`

This spec defines three reinforcing contracts: a lightweight **anchor
schema** pointing memories at external systems of record, first-class
**correction/supersession workflows**, and a **retention policy
vocabulary** for long-horizon memory. All three are designed to work
without any external graph substrate and without breaking the existing
store.

## 1. Source anchor schema

An anchor is a typed, portable pointer from a memory to an external system.
Anchors are a specialization of `external_refs` (full field contract in
`memory-provenance-and-external-refs.md`); this document fixes the
vocabulary and the examples.

```json
{
  "ref_type": "jira_key | confluence_page | slack_thread | meeting |
               customer | repo | pull_request | file | session | url | custom",
  "ref_value": "string, canonical form",
  "source_system": "string, optional (e.g. jira, confluence, github)",
  "relationship": "about | derived_from | mentions | applies_to | supersedes"
}
```

Canonical forms and examples:

| ref_type | Canonical form | Example |
|---|---|---|
| `jira_key` | `PROJECT-123` | `PER-838` |
| `confluence_page` | numeric page id, else full URL | `184726351` |
| `slack_thread` | `channel_id:thread_ts` | `C08A1B2C3:1784492814.260000` |
| `meeting` | `calendar_event_id` or recording URL | `evt_01J9…` |
| `customer` | org/account id in the billing/CRM system | `org_f2aa4822f4bb3407` |
| `repo` | `host:owner/name` | `github:Perseus-Computing-LLC/perseus-vault` |
| `pull_request` | `host:owner/name#N` | `github:Perseus-Computing-LLC/perseus-vault#727` |
| `file` | absolute path, workspace-relative if portable | `/opt/data/config.yaml` |
| `session` | agent session identifier | `hermes:default:3cdf09c0a464` |
| `url` | full URL, scheme required | `https://status.perseus.observer/…` |

Rules:

- **Multiple anchors per memory are allowed.** A memory about a deploy can
  anchor the PR, the session, and the host.
- **Anchors are metadata, not content.** They are queryable/filterable but
  never required; a memory with no anchors is fully valid.
- **Anchors do not imply permission.** Resolving an anchor (fetching the
  Jira issue) is the consumer's problem; Vault stores the pointer.
- **Workspace isolation is unchanged.** Anchors travel with the memory's
  workspace scope.

### 1.1 How anchors affect retrieval and synthesis

- Recall MAY filter or boost by `ref_type`/`ref_value` ("everything
  anchored to this repo/PR").
- Serving explanations SHOULD render the anchor as the "source" cue (see
  `served-memory-api.md`).
- Synthesis flows (briefings, handoffs) SHOULD group by anchor when
  assembling a narrative about one external object.
- Anchors are the supported interop path: they give graph-shaped consumers
  (TWG/ARI-style systems) stable join keys without Vault adopting graph
  storage.

## 2. Correction and supersession workflows

### 2.1 Actors and primitives

- `mimir_correct` — record that an approach was wrong, with the right
  behavior. Creates a tier-1 `correction` class entity and a journal entry.
- `mimir_supersede` — declare that entity B replaces entity A: sets A's
  `status=deprecated`, closes A's valid-time period, and links
  `B -[supersedes]-> A`. Reversible via history (`mimir_as_of`).
- `mimir_forget` / `mimir_purge` — soft-archive and (GDPR-grade) erasure.

### 2.2 The canonical correction flow

1. **Detect.** Agent or operator determines a live memory is wrong.
2. **Record the correction** with `mimir_correct` capturing
   `wrong_approach`, `user_correction`, `task_context`. This is what makes
   the correction outrank the old belief at serve time (taxonomy §3 tier 1).
3. **Supersede the wrong entity** with `mimir_supersede(old → correction
   or replacement fact)`. If the correction introduces a replacement fact,
   write it first and supersede toward it.
4. **Preserve history.** Never edit the old entity's body to make it look
   right. The superseded entity stays queryable via `as_of`/`valid_at` so
   audit can reconstruct "what did we believe when".
5. **Proactively re-test.** The deja-vu guard
   (`mimir_check_failure_pattern`) consults corrections before retrying
   failed approaches.

### 2.3 Marking prior memory wrong vs. superseding stale assumptions

- *Wrong fact* → correction + supersede. The old entity is deprecated but
  retained for audit.
- *Stale assumption* (was true, no longer is) → supersede only, with
  `valid_to` set to when it stopped being true. This is a valid-time
  operation, not an admission of error, and does not create a correction.
- *Disputed but unresolved* → leave both live; serving surfaces the pair in
  the contradictions view (taxonomy rule R5). Do not pick a winner
  silently.

## 3. Retention policy vocabulary

Every memory carries an effective retention policy. The policy is
expressible in existing fields today; this vocabulary makes it explicit and
gives `mimir_decay`/`mimir_compact`/`mimir_prune` well-defined semantics.

| Policy | Meaning | Encoding today |
|---|---|---|
| `keep_forever` | Never decayed, never auto-archived, always eligible to serve. | `mimir_score` ≥ 0.7 (verified + importance floor) |
| `decay_unless_reinforced` | Default. Ebbinghaus decay; retrieval/usefulness reinforcement resists it; auto-archives below threshold. | default `decay_score` behavior |
| `archive_when_superseded` | On supersession, soft-archive instead of retain-as-history. | archive in the supersede flow (opt-in flag) |
| `retain_no_autoserve` | Kept and queryable, but excluded from context injection/briefings. | status or flag consulted by serving layer |
| `erase_on` *(GDPR)* | Hard delete including history and journal payload redaction. | `mimir_forget` + `mimir_purge` |

Rules:

- **Policy is per-entity and inspectable.** Serving explanations must name
  the policy when it affected inclusion/exclusion.
- **Defaults are safe.** Absent an explicit policy, everything is
  `decay_unless_reinforced`.
- **Retention never rewrites history.** `archive_when_superseded` archives
  the live row; the versioned history in `entity_history` is governed only
  by the history-retention knobs, not by serve-side policy.
- **Policies compose with precedence, not against it.** A `keep_forever`
  superseded entity is still demoted by rule R4 — retention controls
  *whether* a memory exists, precedence controls *what gets served*.

## 4. Acceptance checklist traceability

- #721: anchor model (§1), correction/supersession flow improvements (§2),
  retention policy model (§3); follow-on implementation splits: wiring
  `archive_when_superseded` into `mimir_supersede`, and a
  `retain_no_autoserve` serving filter. ✔
- #725: anchor schema (§1), examples (§1 table), retrieval/synthesis
  implications (§1.1). ✔
