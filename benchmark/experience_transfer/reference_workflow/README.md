# Reference workflow: governed Attio record memory

Status: local synthetic design/implementation slice; not deployed and not a claim that
Attio or a live Vault instance exposes every field shown here.

## Proposition demonstrated

A worked prior experience may be useful, but it is not automatically a current
instruction. The workflow permits reuse only after current context, provenance,
evidence, authority, and revalidation checks. It returns an answer/reuse decision,
rejects failed or stale experience, abstains when evidence is unavailable, and blocks
when authority or contradiction is unresolved.

## User journey

1. An operator opens an Attio company/deal/person record.
2. The record identity selects a bounded memory scope: `attio:{object}:{record_id}`.
3. Current workspace context is selected through the reviewed Perseus Context Engine
   file/MCP seam; the local slice represents the resulting current world by a
   committed `world_state_hash`.
4. The Attio record widget requests scoped memory. The real bridge seam is
   `GET /v1/records/{object}/{record_id}/memory`; the current app calls it through
   `app/src/vault-memory.server.ts` with action `read`.
5. Candidate memories carry identity, scope, capture/valid-time, source/evidence
   commitments, and lineage. The local governed adapter verifies those commitments;
   it never treats a retrieved body as self-authenticating.
6. The authority stage checks the captured/current authority version and permitted
   `reuse_experience` action. Rotated or revoked authority blocks before action.
7. The system evaluates current/stale/superseded state, contradiction/split-brain,
   evidence status, failed-approach status, and revalidation. It returns `reuse`,
   `reject`, `abstain`, or `block` with a bounded reason code.
8. The answer-facing path may use the reused experience only for `reuse`. A rejection,
   abstention, or block is not converted into an invented answer or empty success.
9. An inspectable hash-only receipt records stage digests, selected memory IDs,
   source/evidence/authority commitments, decision, reason, and
   `sensitive_payload: not_captured`.
10. A human can drill into Vault history/as-of/valid-at records separately. The receipt
    proves which governed references were considered without publishing their bodies.

## Exact existing product seams inspected

### Perseus Context Engine / Hermes

- `work/perseus-context-runtime/docs/HERMES_INTEGRATION.md`: file render to
  `.hermes.md`, `AGENTS.md`, or another host file; optional stdio MCP server.
- `work/perseus-context-runtime/spec/integration.md`: render/MCP adapter boundary,
  explicit workspace, read-only default, and opt-in service/shell/network behavior.
- Hermes host boundary: a `.hermes.md` or configured MCP server supplies context;
  Perseus does not become a model provider or authorization substitute.

### Perseus Vault

- `perseus_vault_recall`: scoped memory selection.
- `perseus_vault_history`, `perseus_vault_as_of`, `perseus_vault_valid_at`:
  historical and temporal inspection.
- `perseus_vault_supersede` and `perseus_vault_forget`: correction/supersession and
  logical revocation paths.
- `perseus_vault_authority_get` / authority transition surfaces: authority must remain
  a separate decision from provenance.
- `work/perseus-vault-run3-publication/docs/evidence-chain-guidance.md` and
  `docs/integration/context-budget-stack.md`: evidence/serving boundaries and
  orthogonal lifecycle hooks.

### Attio bridge/app

- `work/perseus-attio/bridge/server.py`: record scope, REST routing, scoped recall,
  history/as-of/valid-at, supersede, and forget.
- `work/perseus-attio/bridge/perseus_vault_client/__init__.py`: dependency-free MCP
  stdio client, response normalization, bounded reads, and lifecycle teardown.
- `work/perseus-attio/app/src/vault-memory.server.ts`: server-function actions
  `read`, `write`, `supersede`, `forget`, and `history`; workspace settings hold the
  bridge URL/token.
- `work/perseus-attio/app/src/app/extensions/perseus-vault-memory/memory-panel.tsx`:
  record widget; `perseus-vault-recall` provides the search/forget dialog and
  `perseus-vault-remember` provides the write action.

## Local implementation slice

`reference_workflow/implementation.py` uses the shared synthetic corpus and the
provider-free governed adapter. It emits four receipts covering reuse, failed-
approach rejection, revoked-evidence abstention, and rotated-authority blocking.
It does not start a Vault process, make an Attio request, call Hermes, contact a
provider, or write to production state. The receipt is deliberately narrower than a
live integration response: IDs and commitments are retained; memory/context bodies
are not.

The current Attio bridge is a useful scope/lifecycle transport but does not expose a
hash-only evidence envelope or an authority decision receipt in its normalized hit
shape. The local slice therefore defines the consumer contract that a later bridge
revision would need to satisfy rather than claiming the current bridge already does.

## Acceptance tests

- Four selected cases produce `reuse`, `reject`, `abstain`, and `block` respectively.
- Every receipt has a recomputable SHA-256 commitment and no raw body, prompt, query,
  token value, credential, or provider-response field.
- The receipt’s scope, world hash, source/evidence commitments, authority commitment,
  stage order, and decision are internally consistent.
- Changing a source/evidence/authority hash or scope fails closed.
- A stale experience without a passing current-world revalidation cannot be reused.
- A failed approach cannot be promoted into a reusable current instruction.
- Revoked/deleted evidence yields abstention; split-brain or changed authority yields
  block; no path executes an external action.
- The same corpus and fixed seed regenerate byte-identical receipts.

## Later operator authorization required

- Applying a code/design patch to `perseus-attio` or `perseus-vault`.
- Running a live Vault binary against a temporary integration store beyond this local
  fixture slice.
- Deploying or exposing the bridge, changing `BRIDGE_TOKEN`, TLS, CORS, or network
  bindings.
- Installing or changing Hermes MCP/config/gateway state.
- Connecting Attio developer-console settings, publishing the App Store listing, or
  contacting Attio/CogniCore/any external collaborator.
- Executing a real Agent A/B provider-backed benchmark, judge call, or external adapter.
