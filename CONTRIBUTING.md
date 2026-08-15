# Contributing to Perseus Vault

Thanks for wanting to help! Perseus Vault is a Rust MCP server for persistent AI agent
memory. Contributions of all kinds are welcome — code, docs, bug reports, feature
ideas.

## Development Setup

```bash
git clone https://github.com/Perseus-Computing-LLC/perseus-vault.git
cd perseus-vault

# Build (the pinned Rust 1.97.1 toolchain is selected automatically)
cargo build --locked --release

# Run tests
cargo test --locked

# Run with a test database
cargo run -- --db /tmp/perseus-vault-test.db
```

**Project structure:**

```
src/
  main.rs    — CLI entrypoint, arg parsing
  mcp.rs     — MCP JSON-RPC 2.0 protocol (stdio)
  tools.rs   — Tool implementations (store, recall, health, stats)
  db.rs      — SQLite + FTS5 storage layer
```

## Pull Request Workflow

1. Fork the repo
2. Create a feature branch (`git checkout -b feat/my-feature`)
3. Make your changes
4. Run `cargo test` and `cargo fmt`
5. Push and open a PR against `main`

Keep PRs focused — one concern per PR. If you're fixing a bug, add a test.

**Tool registry changes** (new tools, renames, deletions): every canonical tool
is a public contract. Adding a tool requires a registry entry in `src/mcp.rs`,
a `TOOL_SCOPES` classification, the README tool-family section, and the
metadata count surfaces (README / CLAIMS-AUDIT / manifest / server / glama) —
`scripts/registry_metadata_check.py` enforces the lockstep. Deleting or
merging tools follows the [tool lifecycle policy](docs/specs/tool-lifecycle-policy.md)
and [consolidation & deprecation-alias design](docs/specs/tool-consolidation-deprecation.md).

## Code Style

- `cargo fmt` (standard Rust formatting)
- `cargo clippy` for linting
- Keep functions small and single-purpose
- Add doc comments for public items

## Questions?

Open a [discussion](https://github.com/Perseus-Computing-LLC/perseus-vault/discussions) or file an
issue with the `question` label.
