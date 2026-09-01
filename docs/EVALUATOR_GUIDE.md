# Perseus evaluator guide

Perseus is a three-tier local-first platform. The tiers are composable, but they
have different responsibilities and can be evaluated independently.

## 1. Perseus Context Engine

**Role:** workspace fact scanner and pre-session context renderer.

It resolves operator-selected workspace sources and renders a bounded Markdown
artifact, normally `AGENTS.md`, before an agent session starts. It describes the
current workspace: project instructions, repository state, selected files, and
other explicitly allowed facts. The output is an input artifact for an agent or
MCP host; it is not an inference engine.

Evaluate it by running `perseus quickstart` in a temporary workspace, then
rendering `.perseus/context.md` to `AGENTS.md`, and inspecting the bounded output.

## 2. Perseus Vault

**Role:** durable local memory.

Perseus Vault is an encrypted Rust memory engine backed by embedded SQLite and
FTS5. It stores and recalls durable workspace facts, with optional local dense
embeddings and hybrid retrieval. Its primary agent integration is MCP over stdio;
it does not require a Perseus-hosted service for the local path.

For an LLM host, use the lean advertisement profile to keep tool selection focused:

```bash
mkdir -p /tmp/verify_vault
perseus-vault serve --profile lean --db /tmp/verify_vault/perseus-vault.db
```

The lean profile advertises the core memory operations (`remember`, `recall`,
`forget`, `correct`, `context`, `perseus_vault_workspace_status`, and `health`).
In lean mode, `perseus_vault_workspace_status` is scoped to the transport-stamped
MCP `clientInfo.name` and does not disclose other profile/workspace bindings. The
full registry remains available under the default/all profile. Evaluate the
installed binary and the MCP `initialize`/`tools/list`/`tools/call` flow rather
than relying on a registry count copied from another release.

## 3. Perseus Ledger

**Role:** tamper-evident event provenance.

Perseus Ledger is a stdlib `http.server`-based threaded audit server and Python
package. It records events in a hash-chained append-only history, supports
verification and receipts, and exports OSCAL-compatible evidence. It is the
provenance layer for actions and resource/accounting events; it is not the memory
store and does not replace the Context Engine or Vault.

Evaluate it with the repository's `uv run pytest` suite, then exercise a temporary
SQLite database, append a record, verify the chain, and inspect an OSCAL export.

## Boundary: what Perseus is not

Perseus is **not** an LLM, model provider, inference API, or prompt-generation
service. It does not select or host a model for the operator. Perseus is also
**not a required cloud SaaS dependency**: the Context Engine, Vault's local stdio
path, and Ledger's local deployment can run on operator-controlled machines.
Optional remote transports, provider integrations, and hosted deployment choices
are explicit configuration boundaries, not prerequisites for the local product.

## Recommended evaluation order

1. Run the Context Engine quickstart in `/tmp/verify_perseus`, render its context
to `AGENTS.md`, and inspect the result.
2. Start Vault with `--profile lean` and verify its advertised MCP tools and a
   write/recall round trip using a temporary database.
3. Run Ledger's tests and verify one append, chain check, and OSCAL export against
   a temporary database.
4. Keep results labeled by tier, repository revision, feature profile, and test
   command; do not combine retrieval, context-rendering, and provenance results
   into one product metric.
