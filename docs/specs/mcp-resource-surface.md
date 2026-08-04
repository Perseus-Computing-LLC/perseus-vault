# MCP resource surface for the context engine

Status: design (spec-grounded, not yet implemented)
Date: 2026-08-04
Resolves: #842
Spec basis: MCP 2025-06-18, /server/resources (fetched 2026-08-04)
Related: [FUSE decision record](../decision-record-fuse-filesystem-projection.md)
(#841) · digest semantics (#835) · AAR null-effect and argv policy (#836)

## 1. Layout

The Vault memory store is a (category, key) entity store. The resource surface
mirrors the write boundary: **one resource per entity** — the smallest thing
that changes on its own.

| Surface | URI | Granularity |
|---|---|---|
| Per-entity resource | `perseus://{workspace}/{category}/{key}` | One entity; a single field edit touches exactly one resource |
| Workspace index | `perseus://{workspace}/index` | A small, concrete, subscribable URI listing categories/keys + per-entity state markers |
| URI template | `perseus://{workspace}/{category}/{key}` advertised via `resources/templates/list` | Keeps `resources/list` short while every entity stays addressable (spec's own pattern, cf. `file:///{path}`) |

`resources/list` returns the template plus the concrete index URIs; entity
URIs are enumerated by reading the index, not by inflating `resources/list`.

## 2. Index as notification fan-in

MCP semantics constrain the design:

- `notifications/resources/updated` carries **only the URI** — the client
  must call `resources/read` to fetch contents. Resource granularity is
  literally re-read cost: one blob per workspace would make a single field
  edit re-read everything.
- `resources/subscribe` accepts a **concrete URI only** — there is no template
  subscription in the spec, so a client cannot subscribe to slices that do
  not yet exist.
- New slices appearing is a **list change** — `notifications/resources/list_changed` —
  a separate channel from per-resource `updated`.

Therefore:

1. The client subscribes to the one concrete URI it can name: the workspace
   **index**.
2. On any write, the server emits `resources/updated` for the touched entity
   URI **and** for the index URI, plus `resources/list_changed` when a new
   entity (slice) first appears.
3. The client re-reads the cheap index (a small, bounded document), then pulls
   only the touched entities named by the notifications.

This fixes the review's under-specified seam: the index is **not** a
read-once-at-start resource — it is the highest-churn resource in the system
(its update rate equals the workspace write rate), and the fan-in design is
only sound if the index stays small.

## 3. Churn model

For a workspace with `E` entities and write rate `W` (writes/sec, including
creates):

- per-entity resource update rate ≈ `W/E` (a field edit touches one entity);
- index update rate = `W` — **total** workspace write rate, every write
  bumps the index;
- client re-read amplification per notification batch = 1 index read + k
  entity reads, where k is the number of distinct entities touched since the
  last index read;
- index size is bounded by `E` (entity count), **not** by `W`, so index
  re-reads stay cheap even as write rate grows — but this is a bounded
  *claim to be measured*, see §5.

## 4. Notification channels (summary)

| Channel | Carries | Means | Client action |
|---|---|---|---|
| `notifications/resources/updated` | URI of the changed resource (entity or index) | A slice changed | Read the touched entity/index |
| `notifications/resources/list_changed` | (no URI) | A new slice appeared | Re-read the index to enumerate new URIs |

The two channels are distinct by spec; the design relies on both, and neither
carries content — only the signal to re-read.

## 5. Bounded-load measurement plan

No scaling claim is made until this plan is executed. The plan bounds the one
claim the layout depends on: **index re-read amplification is acceptable at
fleet write rates**.

Harness: a synthetic load generator driving the MCP server with `N` writers
(per-entity writes + creates across one or more workspaces) and `M` clients
subscribed to workspace indexes, re-reading per the fan-in protocol.

Loads: 1×, 10×, and 100× a modeled fleet write rate `W_fleet` (modeled from
observed per-workspace write rates, not from a benchmark battery).

Metrics, recorded per load:

- index read latency p50/p95 (client-observed `resources/read` on the index);
- amplification factor: index + entity reads per write, client-observed;
- notification delivery: counts of `updated` vs `list_changed`, and any
  dropped/duplicated notifications;
- index document size as a function of `E` and `W` (verifying the bounded-by-E
  claim).

Pass criteria (proposed, to be confirmed by the executed run): p95 index read
latency within the same order of magnitude at 100× fleet rate as at 1×;
amplification ≤ 1 index read + k entity reads with k ≪ E per batch; index
size growing with `E`, flat in `W`. A run that fails any criterion stops the
adoption of the index-as-fan-in layout until the layout is revised.

## 6. Non-goals

- **No template subscriptions.** The spec has no template subscription; the
  index-as-fan-in design exists precisely because slices cannot be subscribed
  before they exist.
- **The Ledger is not forced into this model.** The Ledger is an append-only
  stream, not a mutable resource surface; receipts stay on the tool boundary.
- No change to the MCP tool surface; this design covers resources only.
- Digest semantics (#835) apply unchanged: resource contents carry byte
  identity; supersession/archived state is surfaced as metadata, never
  inferred from a digest.

## 7. Open items before implementation

- Confirm the pass criteria thresholds against the first executed load run.
- Decide index payload shape (categories/keys only vs. per-key state markers)
  against index-size measurement.
- Map `resources/subscribe` lifecycle (unsubscribe on workspace teardown).
