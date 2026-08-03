# Perseus Vault upgrade and migration playbook

This playbook upgrades a local source-built installation without changing the
location of its durable database. It is deliberately explicit about the
boundary between the binary, the client configuration, and the database. It
does **not** promise a generic automatic database migration: do not invent a
migration command or copy a newly initialized empty database over an existing
one.

The normal upgrade path below keeps the same database and passes its absolute
path to every command. The existing `migrate --from ... --to ...` command is a
separate, format-specific path for a legacy v0.1.x source database and an
explicit target; it is not a generic command for every binary upgrade.

## 1. Preflight and backup

Stop or disconnect every MCP client and server process that can write the
store before copying it. A live SQLite/WAL copy is not a restore proof. Choose
one absolute path and use it consistently:

```bash
set -euo pipefail

SRC="/path/to/perseus-vault"
PROJECT="/path/to/agent-project"
DB="/absolute/path/to/perseus-vault.db"
BIN="$HOME/.local/bin/perseus-vault"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$HOME/.perseus-vault/backups/$STAMP"

mkdir -p "$BACKUP_DIR"

# Refuse to update a checkout with unreviewed local edits.
test -z "$(git -C "$SRC" status --porcelain)"
git -C "$SRC" rev-parse HEAD > "$BACKUP_DIR/source-before.txt"
"$BIN" --version > "$BACKUP_DIR/binary-before.txt"

# Copy the database only after all writers have stopped. Keep SQLite companions
# when present so the backup set is self-contained.
for suffix in "" "-wal" "-shm"; do
  source="${DB}${suffix}"
  if [ -e "$source" ]; then
    cp "$source" "$BACKUP_DIR/$(basename "$source")"
  fi
done
```

If the database is encrypted, back up the key file in the same protected
backup set. Never put its contents in a ticket, terminal capture, or evidence
bundle:

```bash
KEY="/absolute/path/to/secret.key"
cp "$KEY" "$BACKUP_DIR/$(basename "$KEY")"
chmod 600 "$BACKUP_DIR/$(basename "$KEY")" 2>/dev/null || true
```

Run the keyless doctor check **after** the database backup. `doctor` reports
the actual on-disk encryption state and the selected database; it is not a
substitute for a backup:

```bash
"$BIN" doctor --db "$DB" | tee "$BACKUP_DIR/doctor-before.txt"
```

Record the backup identity without recording memory bodies or key material:

```bash
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$BACKUP_DIR/$(basename "$DB")" > "$BACKUP_DIR/backup.sha256"
else
  shasum -a 256 "$BACKUP_DIR/$(basename "$DB")" > "$BACKUP_DIR/backup.sha256"
fi
```

## 2. Update the source checkout

Use a clean, reviewed checkout or pin the exact reviewed tag/commit. For a
checkout that tracks `main`, the source update is:

```bash
git -C "$SRC" fetch --prune origin
git -C "$SRC" switch main
git -C "$SRC" pull --ff-only origin main
git -C "$SRC" rev-parse HEAD | tee "$BACKUP_DIR/source-after.txt"
```

Do not describe this step as a data migration. The current source owns its
schema initialization behavior when it opens a database; the safe operator
contract is to keep the backup, use an explicit `--db`, and verify the result.
For the separate legacy v0.1.x format only, inspect the current command help and
use the explicit source/target form when applicable:

```bash
"$BIN" migrate --from /absolute/path/to/old.db \
  --to /absolute/path/to/migrated.db
```

Do not run that command for an ordinary current-database upgrade merely because
the binary changed.

## 3. Build and install the binary

Build from the reviewed source, then copy the resulting binary to the required
canonical install location. Save the previous binary for rollback before
overwriting it:

```bash
cd "$SRC"
cargo build --locked --release

mkdir -p "$HOME/.local/bin"
if [ -f "$BIN" ]; then
  cp "$BIN" "$BACKUP_DIR/perseus-vault.previous"
fi
cp target/release/perseus-vault "$BIN"
chmod +x "$BIN"
```

**macOS requirement:** after **every** rebuild or replacement of the binary,
re-sign the installed file before running it. This prevents the Apple Silicon
`Killed: 9` launch failure. The ad-hoc signature is a local launch workaround;
Developer ID signing/notarization for release artifacts is a separate CI path.

```bash
if [ "$(uname -s)" = "Darwin" ]; then
  codesign --force --sign - "$BIN"
fi
```

Verify the installed identity before touching the client configuration:

```bash
"$BIN" --version
"$BIN" doctor --db "$DB"
```

## 4. Keep the database path and encryption explicit

Pass `--db "$DB"` to `doctor`, `prepare`, `serve`, and
`install-client`. Do not rely on implicit default-path precedence during an
upgrade: an existing compatibility database can otherwise be selected when a
different path was intended.

For an encrypted database, keep using the existing key and verify a
key-backed read; `doctor` itself does not require the key and does not reveal
key material:

```bash
"$BIN" doctor --db "$DB"
"$BIN" prepare --db "$DB" --encryption-key "$KEY" \
  --task "post-upgrade verification" --json
```

Do not use `init` as a generic upgrade step. If a plaintext database is being
intentionally converted to encryption, follow [ENCRYPTION.md](../ENCRYPTION.md),
back up first, and use the current command's explicit paths (including
`--rekey` when existing plaintext bodies are meant to be encrypted):

```bash
"$BIN" init --db "$DB" --key-file "$KEY" --rekey
```

Back up the newly created key immediately. A lost key cannot recover encrypted
bodies. Treat a doctor warning about mixed or missing encryption state as an
operator action, not as permission to continue with mixed plaintext writes.

## 5. Preview and install client wiring

Run `install-client` from the intended agent project directory because
project-scoped config and instruction files are resolved from the current
working directory:

```bash
cd "$PROJECT"
```

Choose one client or use `--all-detected` as documented by the CLI. The dry run
prints every file and diff it would touch and writes nothing:

```bash
CLIENT="claude-code"  # or codex, cursor, claude-desktop, hermes, windsurf, vscode, zed
"$BIN" install-client --client "$CLIENT" --db "$DB" \
  --hooks --rules --dry-run
```

Review the dry-run paths, especially the database path, binary path, hooks, and
instructions file. Then run the same command without `--dry-run`:

```bash
"$BIN" install-client --client "$CLIENT" --db "$DB" \
  --hooks --rules
```

If encryption is enabled, append `--encryption-key "$KEY"` to both commands.
`install-client` merges rather than overwrites supported config formats. Before
each changed file it writes a `<file>.bak-perseus` backup; an idempotent no-op
does not rewrite the file. Record the backup paths printed by the command and
copy those backup files into `"$BACKUP_DIR"` before starting the client again.

## 6. Restart the client and run an MCP smoke test

Fully exit and restart the MCP host/client after configuration changes. This is
needed for it to respawn the server with the new command and database path;
reloading only an already-running conversation is not enough.

First exercise the same stdio handshake used by an MCP client. The smoke test
uses only the canonical health tool and closes stdin so the server exits cleanly:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"migration-smoke","version":"1"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"perseus_vault_health","arguments":{}}}' \
  | "$BIN" serve --db "$DB"
```

For an encrypted database, add `--encryption-key "$KEY"` to the final
`serve` command. Confirm that the responses contain a successful initialize,
a `tools/list` entry for `perseus_vault_health`, and a successful health result;
an MCP error or a missing canonical tool is a failed smoke test. Do not treat a
process that merely starts, or a client configuration that merely parses, as
proof that the intended database was opened.

## 7. Rollback

Rollback is safest before allowing the new binary to perform writes. Stop the
client and server first. Restore the previous binary and re-sign it on macOS:

```bash
cp "$BACKUP_DIR/perseus-vault.previous" "$BIN"
chmod +x "$BIN"
if [ "$(uname -s)" = "Darwin" ]; then
  codesign --force --sign - "$BIN"
fi
```

If the new binary opened the database and the schema or data must be reverted,
restore the complete pre-upgrade backup set—not just the binary. Only do this
when you accept losing writes made after the backup:

```bash
cp "$BACKUP_DIR/$(basename "$DB")" "$DB"
for suffix in "-wal" "-shm"; do
  backup="$BACKUP_DIR/$(basename "${DB}${suffix}")"
  if [ -e "$backup" ]; then
    cp "$backup" "${DB}${suffix}"
  else
    rm -f "${DB}${suffix}"
  fi
done
```

Restore the client configuration from the pre-upgrade copy or the matching
`.bak-perseus` file produced by `install-client`, then run `doctor` and the MCP
smoke test again with the same explicit `--db` (and key, if applicable). A
binary-only rollback is not proof of database compatibility; when unsure,
restore the verified database backup before reopening it with the old binary.

## 8. Evidence and privacy checklist

- [ ] Record the source commit before and after the update, installed
      `perseus-vault --version`, explicit database path, and command exit status.
- [ ] Keep the database backup and any SQLite companion files together; record a
      checksum of the backup, but do not upload the database or its raw output.
- [ ] Protect the encryption key backup separately. Never paste key contents,
      client tokens, or `--encryption-key` values into logs or issue reports.
- [ ] Save `doctor` status and the bounded MCP smoke verdict, not full memory
      bodies, prompts, transcripts, or client logs.
- [ ] Review `install-client --dry-run` before writing; retain the printed
      `.bak-perseus` paths and treat config diffs as potentially sensitive.
- [ ] Confirm that all clients point at the same intended absolute `--db` path
      and that no empty replacement database was created by an implicit default.
- [ ] If exports, derived projections, backups, or host context contain the
      affected records, handle their retention/erasure separately; a database
      purge does not erase copies outside the database.
- [ ] Remove temporary smoke-test captures and unneeded backup copies according
      to the operator's retention policy. A checksum is evidence of identity,
      not evidence that a restore has been tested.

This procedure is local-only. It does not publish, commit, or mutate a remote
repository, and it does not claim that a successful process start proves
semantic compatibility for every client or database history.
