# Fail-loud test gating

Status: normative. Applies to every test in the Rust workspace, `tests/`
integration suites, and the Python conformance harness.

Borrowed from RunAlphaLoop/verity's testing posture (HONESTY.md): a test that
needs required infrastructure must **fail loudly with an actionable message**,
never silently return — a red pipeline is the cheapest signal there is, and a
silently-skipped enforcement test is a hole wearing a green badge.

## The two classes

1. **Enforcement-soundness tests** — anything that proves a boundary holds:
   visibility gates, leak harnesses, sandbox proofs, permission/ACL checks,
   dedup/interference invariants, fail-closed paths, WAL durability.
   Required infra missing → `panic!` with the missing dependency and how to
   provide it. Never skip, never `return` early, never `#[ignore]` without an
   active tracking issue.
2. **Optional suites** (perf, best-effort telemetry) may skip, but the skip
   line must be labeled and the reason eprintln'd (`skipping <name>: <why>`),
   so a log scan always shows what did not run.

## How CI provides the deps

The suite must not need anything the pipeline cannot provide deterministically:

- External services are faked in-process (`spawn_fake_embed_server` for
  embedding) or spawned as fixtures (`tests/*.rs` spawn the real binary with
  explicit `--db` paths) — a missing service is never a skip condition.
- OS capabilities are the one legitimate environment branch (`unshare -n`
  sandbox proofs): both directions are tested — `sandbox_actually_blocks_sockets`
  (feature present) and `c8_unverified_without_unshare` (feature absent) — so
  no environment silently demotes the boundary to untested.
- Anything that becomes configurable later (API keys, model endpoints) must
  come with a fake or a required-in-CI fixture, not a conditional return.

## Audit (2026-08-12, #998)

Sweep of all `std::env::var`-gated tests, `#[ignore]` markers, and
`eprintln!(*skip*)` early returns: the suite already conforms.

- Embedding tests: fake in-process server, zero live-service dependency.
- CLI integration tests: binary spawned with explicit env — no skips.
- `tests/test_integration_conformance.py`: zero `skipif` markers.
- Single skip in the tree: `verify.rs` sandbox proof, OS-capability branch
  (`unshare` absent), bilaterally covered as above — compliant by design.

New tests must follow the two classes; reviewers treat a bare `return` inside
a `#[test]` as a defect unless it carries a labeled skip for an optional suite.
