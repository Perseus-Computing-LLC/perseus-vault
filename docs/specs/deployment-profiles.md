# Feature Spec: Deployment profiles (#870)

**Status:** implemented (registry 103)
**Depends on:** readiness (#677), LLM/embedding config, connectors, encryption state, offline mode
**Competitive driver:** AIMAOS's operationally legible local-first posture vs Helix's many cloud/external channels

## Problem

Perseus documents local-first and air-gap behavior, but operators need ONE
explicit, machine-readable deployment profile stating which model, embedding,
connector, network, cloud, and action paths are active — described from
**actual runtime state**, not configuration intent.

## Profiles

| Profile | Meaning |
|---|---|
| `offline` | `--offline` air-gapped: only `mcp_stdio` listener, zero egress, providers + connectors disabled at startup |
| `local_only` | All listeners loopback, zero egress |
| `local_with_approved_network` | Egress exists only to operator-configured endpoints (LLM/embedding providers, connectors) — approved by config |
| `external_actions_enabled` | Explicit opt-in (`PERSEUS_VAULT_EXTERNAL_ACTIONS=1`); the vault itself never mutates external systems |

Derivation (in order): offline flag → external-actions opt-in → egress empty →
approved network. Egress = non-loopback hosts from the enabled LLM endpoint,
the embedding provider endpoint, and remote connectors (`remote_host`).

## Components (all runtime-derived)

- **model_backend**: `bundled` \| `ollama` (loopback endpoint) \| `provider` \|
  `none`; model name; available; degraded.
- **embedding_backend**: `bundled` (compiled-in ONNX) \| `provider` \|
  `none`; available; **degraded** — a configured-but-unusable local backend
  (provider endpoint set while the LLM integration is off; bundled backend
  disabled) is reported explicitly, NEVER silently reclassified as empty
  success; plus the `semantic_recall` cross-check from readiness.
- **network**: listeners (`mcp_stdio` always; `web_dashboard(bind)`; `grpc`),
  `egress_hosts` (hosts only — sanitized, no URLs/tokens/raw bodies),
  `loopback_only`.
- **connectors**: name, remote flag, remote host (`Connector::remote_host()`,
  default None; GitHub connector reports `api.github.com`).
- **cloud_provider_use**: `none` or comma-joined non-loopback hosts.
- **external_mutations**: `disabled` \| `enabled` (startup snapshot of the
  opt-in env — deliberately not a live read, so concurrent profile calls
  never race a process global).
- **encryption**: `at_rest` (aes_256_gcm \| plaintext — the session's write
  path), `storage_state` (disk probe: encrypted \| plaintext \| mixed-legacy),
  `in_transit` (loopback_only \| operator_configured).
- **raw_retention**: `memory_bodies=retained_at_rest` (the store, encrypted at
  rest) and `raw_logs=digest_only` (journal/audit records carry sha256
  digests, never raw bodies).

## Runtime snapshot

`serve` captures the EFFECTIVE flags after offline-mode zeroing
(`set_deployment_context`): `--offline` disables web dashboard, LLM,
embedding endpoint, and connectors before the snapshot, so the profile
describes what actually runs, not what the config file intended.

## Surfaces

- `perseus_vault_deployment_profile` (new MCP tool, registry 103) — the full
  resolved profile, read-only.
- `perseus_vault_health` — gains `deployment_profile`.
- `perseus-vault doctor` — prints the resolved profile.
- Benchmark run manifests — `benchmark/telemetry/run.py` embeds the profile
  (sanitized) in its report.

## Acceptance (verified in tests)

1. Offline: profile `offline`, only `mcp_stdio`, zero egress, and FTS5 /
   bundled hybrid / fused (graph+temporal+dense arms) recall all complete
   with zero network (the sandbox has none — success is the proof).
2. Local-only: loopback web listener, `cloud_provider_use: none`.
3. Approved network: provider egress reported as a bare host (no URL paths
   leak), profile `local_with_approved_network`.
4. Degraded reporting: provider configured + LLM off → `degraded: true,
   available: false` (lite build); bundled build stays available with
   `semantic_recall` honest (`no_coverage` on a fresh store).
5. External mutations require the explicit opt-in; default `disabled`.
6. Encryption + retention fields present; health and the tool expose the
   same resolved profile; doctor prints it.

## Out of scope

- Per-request live backend probing (recall outcomes already carry
  `query_embedding_available` per call — the profile is a posture snapshot).
- grpc listener wiring (serve does not start grpc today; the context field
  is ready).
