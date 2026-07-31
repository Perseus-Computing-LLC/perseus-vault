# Artifact manifests and exact evidence retrieval

Status: implementation contract
Date: 2026-07-31
Resolves: #811
Related: `memory-provenance-and-external-refs.md`, `served-memory-api.md`, `source-anchors-corrections-retention.md`

This contract adds a narrow immutable-artifact spine to Vault:

- full SHA-256 content identity
- preserved original bytes
- scope-bound metadata bindings
- compact deterministic manifests by default
- exact bounded excerpt retrieval
- exact candidate-value verification against original bytes

## 1. Identity and storage model

Artifacts split into two layers.

### 1.1 Shared content store

`artifacts` stores the preserved original bytes once per full content hash:

- `sha256` — full 64-hex SHA-256, the durable identity
- `content_b64` — exact original bytes, optionally encrypted-at-rest under the existing DB key path
- `byte_length`
- `created_at_unix_ms`

No truncated hashes are durable keys.

### 1.2 Scope/provenance bindings

`artifact_bindings` stores visibility-safe metadata that can vary by scope even when bytes are identical:

- `binding_id` — deterministic digest of content identity + binding metadata
- `sha256` — foreign key into `artifacts`
- `mime_type`
- `workspace_hash`
- `agent_id`
- `visibility`
- `origin_json` — existing `origin` contract
- `external_refs_json` — existing `external_refs` / anchor contract
- `retention_policy`
- representation semantics:
  - `representation_kind` = `original | derived`
  - `derived_from_sha256`
  - `derivation_kind`
  - `derivation_version`
- `created_at_unix_ms`

Same bytes may therefore be registered once and bound in multiple workspaces without turning the hash into an access grant.

## 2. Scope and visibility

Reads apply filters in this order:

1. exact `workspace_hash` binding match; omitted means global (`""`) bindings only
2. existing `visibility` / `agent_id` read rules (`private`, `fleet`, etc.)
3. only then manifest/excerpt/verification rendering

Artifacts are pointers, not access grants. Knowing a hash alone does not reveal another workspace's binding.

## 3. Manifest contract

Default serving path: compact deterministic manifest.

Returned fields:

- `sha256`
- `byte_length`
- `structure`
  - `utf8_text`
  - `line_count` when UTF-8
  - `trailing_newline` when UTF-8
- `significant_signals`
- `available_retrievals`
  - `byte_range`
  - `line_range`
  - `verify_value`
- `visible_binding_count`
- `bindings[]`
  - scope / visibility
  - MIME type
  - origin / external refs
  - retention policy
  - representation semantics
  - `why_served`

Binding order is deterministic: `created_at_unix_ms ASC, binding_id ASC`.

## 4. Exact retrieval operations

### 4.1 `perseus_vault_artifact_excerpt`

One range kind only:

- byte range: half-open `[byte_start, byte_end)`
- line range: 1-indexed inclusive `[line_start, line_end]`

Returns:

- exact byte anchors
- line anchors when the artifact is UTF-8 text
- `content_b64`
- `content_utf8` when the selected bytes decode cleanly

### 4.2 `perseus_vault_artifact_verify_value`

Performs bounded exact-match search only.

- no regex
- no fuzzy matching
- no inferred counts

Input candidate is `utf8` or `base64`; matching is against preserved original bytes.
Returned anchors are exact byte spans into the original artifact.

## 5. Limits and safety

Current first-slice limits:

- registration size: 8 MiB max source file
- excerpt size: 8 KiB or 200 lines max
- verification candidate: 4 KiB max
- verification result count: 20 max
- `external_refs`: 32 max
- compressed containers (`gzip`, `zip`) rejected in this first slice rather than implicitly decompressed, so byte anchors always refer to original bytes

No unbounded regex or arbitrary programmatic search runs over artifacts in this path.

## 6. Reused conventions

This slice reuses existing Vault contracts rather than inventing parallel metadata:

- provenance: existing `origin`
- source anchors / external pointers: existing `external_refs`
- visibility / owner semantics: existing `visibility` + `agent_id`
- retention vocabulary: existing retention policy names
- served explanation style: `why_served` with anchors after post-filtering

## 7. Follow-on boundary

This contract is the artifact spine for #812 log digests.
Derived digests must bind back to the immutable source with:

- `representation_kind = derived`
- `derived_from_sha256 = <full source hash>`
- versioned derivation metadata

The digest is navigation over preserved evidence, never a replacement for the original bytes.
