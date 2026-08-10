# Read-Only TUI Inspector (#918)

Status: implemented. New CLI subcommand `perseus-vault inspect`, gated
behind the `tui` feature (default ON; `--no-default-features` builds drop
it). New dependency: ratatui 0.29 + crossterm 0.28 (lockfile updated). No
new MCP tools (registry unchanged at 122), no schema change.

## Motivation

Operators inspect Vault state via CLI/MCP/exports. A default-on web server
cuts against the air-gap/SCIF posture, so the inspector is a **terminal
app** that opens the vault database strictly read-only — no migrations, no
write path, no listening sockets. Repair actions deliberately reuse the
governed MCP tools (forget / supersede / quarantine / promote); the
inspector never mutates.

## Surfaces

- **Overview** — entity totals by state (active / archived / quarantined /
  superseded), claim-card count (#852: entities whose decrypted body
  carries `claim_card_version`, or tagged `contradiction`), decay-score
  histogram over active entities, top 20 categories, recall-arm telemetry
  totals (`served_events` / `recall_arm_audits` / `displacement_events`,
  schema v31).
- **Entities** — browsable list (500-row cap, ordered by last access) with
  state badges and decay scores; `f` cycles the state filter (all / active /
  archived / quarantined / superseded / claims). Enter opens the detail
  pane: decrypted body, metadata (source, layer, epistemic/efficacy state,
  visibility, workspace, agent), links, and the full **bi-temporal
  history** (`entity_history` — valid-time [valid_from, valid_to),
  transaction-time recorded/invalidated, supersedes/superseded_by edges).
- **Telemetry** — recent served events (mode/category/key/tokens/query),
  recall-arm audits (candidates/re-entry/delivered), displacement events
  (reason, sole-evidence flag).

## Read-only guarantees

- The connection opens with `SQLITE_OPEN_READ_ONLY` and
  `PRAGMA query_only=ON`; any accidental write fails loudly (covered by
  `open_ro_rejects_writes`).
- No schema migrations run; the inspector tolerates absent tables
  (telemetry tables on pre-v31 databases read as empty).
- **Encryption:** bodies are decrypted only when a key is available
  (`--key-file`, falling back to `$PERSEUS_VAULT_KEY_FILE` — the same env
  the server honors), using the current AAD scheme
  (`len(category):category:key`) with the legacy fallback. Without a key,
  plaintext rows are shown as-is and ciphertext-at-rest rows are flagged
  `(encrypted at rest — pass --key-file to decrypt)`; raw ciphertext is
  never surfaced. Failed authentication surfaces
  `(decryption failed — key mismatch)`.

## Usage

```
perseus-vault inspect --db /path/to/vault.db [--key-file /path/to/secret.key]
```

Keys: `q` quit · `Tab`/`Shift-Tab`/`1-3` sections · `↑/↓`/`j`/`k`
navigate · `Enter` detail · `Esc` back · `f` state filter · `r` refresh.

## Layout

- `src/inspect.rs` — read-only data layer (always compiled; unit-tested in
  both feature lanes).
- `src/tui.rs` — ratatui shell (`cfg(feature = "tui")`).
- `src/main.rs` — `Inspect` subcommand + dispatch.

## Verification

- Unit tests (data layer, both feature lanes): read-only enforcement;
  overview counts (archived / quarantined / superseded / telemetry
  totals); state/category/text filters; detail history + links; encrypted
  bodies flagged without a key and decrypted with the current AAD scheme;
  claim-card detection.
- Default suite 869 passed / 0 failed; no-default suite 861 passed (tui
  feature off — the data layer still compiles and tests).
- Quality harness unchanged (52 cases — the inspector is not
  MCP-observable); registry unchanged (122).

## Out of scope

- The optional localhost web view (issue proposal) is deferred: any HTTP
  surface needs explicit opt-in + token auth design (SCIF posture), and
  the TUI covers the operator workflow. Tracked as follow-up.
- Repairs/writes of any kind.
