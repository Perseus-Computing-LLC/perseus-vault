# Learned anticipation — self-tuning recall_when/preload triggers from usage feedback (#875)

## Problem

Anticipation quality is unmeasured. A `recall_when` trigger can fire uselessly
(wasted context, dilution) or miss (unprepared agent) with no feedback loop.
`recall_when()` and `context_block()` serve preloaded sets deterministically;
nothing records whether a preloaded memory was subsequently **used** by the
agent turn, so trigger selection can never improve. #872 measures
serving-concentration; this issue is the *predictive* complement: preload
precision and recall, plus operator-gated trigger tuning.

## Design

Read-only telemetry first, then an operator-approved review pass — mirroring
the `mental_model_review` governed pattern (#886). All mutations flow through
the audited path (journal + `entity_history` provenance + revision bump);
never silent self-modification (#863/#865 authority-aware).

### "Used" signal

A preload event for entity E at time T is **used** iff E's
`last_accessed_unix_ms` later exceeds the value captured at serving time
(`la_before`). Serving itself never bumps `last_accessed`:
`recall_when()` uses a direct SELECT, and every `context_block` recall arm
passes `skip_side_effects: true` — so `la_before` captured *after* the
serving call excludes the serve and any later read touch (get_entity, recall,
ask grounding, journal refs) counts. No read-path changes.

A **missed** entity in a session: an entity whose `last_accessed` advanced
inside the session window but was **not** in that session's preload set
(cap 200 per session; the window is the same resolution window).

### Schema v36

- `preload_events` — one row per preloaded item per serve:
  `id` (`pl-…`), `ts`, `context_hash` (sha256 of context/query),
  `entity_id`, `trigger_ref` (matched trigger string, or
  `__always_on__` / `__keyword__` / `__context_block__`), `workspace_hash`,
  `session_id` (`''` when unknown), `la_before`, `used` (NULL until
  resolved → 0/1), `resolved_ts`. Index on `(used, ts)`.
- `preload_sessions` — per-session resolution rows: `session_id` (or
  pseudo-session `ctx-<hash8>` when unknown), `anchor_ts`, `preloaded_n`,
  `used_n`, `missed_n`, `precision`, `recall`, `miss_rate`, `resolved_ts`.
- `preload_proposals` — operator queue: `id` (`pp-…`), `entity_id`,
  `trigger_ref`, `suggestion` (`retire` | `add_trigger`), `rationale` JSON
  (served/used/precision/missed_by_trigger/word), `state`
  (`pending|approved|dismissed|applied`), `created_ts`, `decided_ts`,
  `decided_by`, `applied_ts`, `journal_event_id`. One pending proposal per
  `(entity_id, suggestion)`.

### Resolution

`db.preload_resolve(window_minutes, now_ms)` — deterministic. Events older
than the usage window (default 30 min; `PERSEUS_VAULT_PRELOAD_WINDOW_MS` env
override exists for harnesses) are resolved (`used` = touch-after-serve AND
before the resolution sweep — the sweep is the session boundary; touches
after resolution can never credit, since events resolve exactly once), then
grouped into sessions (by `session_id`, else pseudo-session per
`context_hash`) and rolled into `preload_sessions`. `missed` = entities read
since the session's anchor that were not preloaded (cap 200/session).
Per-trigger aggregates are computed on the fly from events + sessions.

Per-trigger metrics (separate from #872 serving-concentration):

- `precision = used / served` — of the times this trigger preloaded its
  memory, how often was it used.
- `recall = used / (used + missed_by_trigger)` where `missed_by_trigger` =
  missed entities whose own `recall_when` (or the trigger under evaluation)
  matches the session context — the trigger *should* have fired but the
  entity was not served (limit truncation, suppression, ordering).
- `miss_rate` (session level) = `missed / (used + missed)`.

### Proposals (offline pass — `perseus_vault_preload_propose`)

- `retire` — trigger served ≥ `PERSEUS_VAULT_PRELOAD_MIN_SERVED` (3) with
  precision < `PERSEUS_VAULT_PRELOAD_RETIRE_PRECISION` (0.25). Rationale
  carries served/used/precision. Apply = remove that trigger string from the
  entity's `recall_when` (others untouched).
- `add_trigger` — entity used in ≥ 2 resolved sessions, never preloaded, has
  no `recall_when`; propose the most frequent meaning-bearing word across
  those sessions' contexts (stopword-filtered, ≥ 3 chars). Apply = append
  `recall_when: [word]` to the body (other fields byte-preserved).
- **Refire-frequency** (issue's third suggestion) is surfaced as telemetry
  only — `missed_by_trigger` per trigger in stats — with no mutation
  behind it (a trigger that matches but isn't served is a limit/suppression
  condition the operator addresses via the existing efficacy tooling;
  documented deviation to keep the mutation surface honest and bounded).
- Proposals only from resolved events; dedup: one pending row per
  (entity, suggestion); re-propose allowed after apply/dismiss.

### Review (`perseus_vault_preload_review`)

- `list` — pending proposals with full rationale.
- `approve(id)` — applies through the governed path: journal event
  `preload_tuning_applied` (proposal id, entity id, suggestion, trigger),
  `entity_history` provenance row (`curated_by="preload-tuning"`,
  `source_ids=[proposal]`, revision bump, audited versioning identical to
  mental-model curation), proposal → `applied`. `retire` rewrites
  `recall_when`; `add_trigger` appends. No other body fields change.
- `dismiss(id, reason)` — proposal → `dismissed`, journaled.
- Fail-closed: unknown id, non-pending state, malformed body → error; no
  silent path exists (proposals table is the only entry to apply).

### Surface (MCP; registry 115 → 119)

- `perseus_vault_preload_resolve` — telemetry bookkeeping pass (window +
  fold into sessions); never touches entity bodies.
- `perseus_vault_preload_stats` — read-only; per-trigger precision/recall/
  missed, per-session rows, overall miss rate; filters `limit`, `since_days`.
- `perseus_vault_preload_propose` — runs the offline pass, writes pending
  proposals (propose stage, journaled `preload_proposals_created`), returns
  the new queue.
- `perseus_vault_preload_review` — `list` / `approve(id)` / `dismiss(id,
  reason)` (commit stage; governed as above).
- `recall_when` / `context` gain an optional `session_id` for attribution.

No CLI verb (matches the operator-review pattern of #886 — review surfaces
are MCP-only); session capture keys (`session-…`) are the session anchor.

## Acceptance coverage (issue success criteria)

- [x] Preload precision/recall per trigger and per session, separate tables
      from #872 serving-concentration telemetry.
- [x] Fixture: low-utility trigger (served ≥ 3, precision < 0.25) flagged by
      the review queue; approved → trigger retired (journal + provenance);
      subsequent `recall_when` no longer serves it.
- [x] Missed-recall rate measured (session recall/miss_rate) and reduced
      against baseline in the harness (retire the noisy trigger → precision
      up; add_trigger → recall up).
- [x] All trigger mutations governed (journal + entity_history provenance +
      operator approval); stats/propose never touch entities.
- [x] Tests: recording on recall_when + context_block, touch-after-serve
      resolution, per-session + per-trigger math, missed attribution,
      proposal thresholds + dedup, approve/dismiss apply + fail-closed,
      no-silent-mutation, session vs pseudo-session grouping.
