# Sourcey MCP reference

This directory builds the static Perseus Vault MCP reference consumed by the
`perseus.observer` website deployment.

The snapshot step starts a lean local Vault build and writes three artifacts:

- `mcp.raw.json`: the unmodified, hashable live MCP snapshot;
- `mcp.render.json`: a Sourcey-compatible derivative with a user-facing stdio
  connection example; and
- `metadata.json`: source commit, Vault version, feature profile, generator
  versions, tool count, and the raw snapshot SHA-256 digest.

Sourcey 3.6.5 cannot generate examples for conditional `allOf` branches. The
rendering derivative therefore omits those branches from the schemas for
`perseus_vault_journal` and `perseus_vault_admission_decide`. The raw snapshot
retains them unchanged. Generation fails if raw and rendered snapshots do not
contain the same ordered set of unique tool names.

From the repository root, build the same reference as CI with:

```sh
cargo build --no-default-features
cd sourcey-docs
pnpm install --frozen-lockfile
pnpm snapshot
pnpm build
```

Pull requests validate the reference. Pushes to `main`, version tags, and
published releases regenerate the downloadable artifacts for final assembly
and deployment by the `Perseus-Computing-LLC/perseus` website repository.
