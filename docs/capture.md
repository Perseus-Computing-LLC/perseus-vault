# In-Session Memory Capture (#520)

Perseus Vault can distill a session transcript or insight payload into
durable memory entities **the moment a problem is solved**, instead of
waiting for a scheduled harvest to notice. This is the capture pipeline:
one code path exposed two ways —

- **CLI:** `perseus-vault capture` (stdin or `--file`)
- **MCP:** `perseus_vault_capture` (aliases `mimir_capture`, `mneme_capture`)

**Off by default, local-first.** Nothing captures automatically: no config
flag turns this on in the background, and the default distiller makes zero
network and zero LLM calls. Capture happens only when you (or a lifecycle
hook you wrote) explicitly invoke the verb or the tool. All writes go to
your local SQLite database.

## What it does

1. **Splits** the payload into candidate notes. Three shapes, auto-detected:
   - **JSONL** — one note per record (uses its `content`/`text`/`insight`/
     `lesson`/`summary`/`message` field, else the compact record);
   - **Headed markdown** — one note per `#`-headed section;
   - **Plain text** — one note per blank-line-separated paragraph.
   Trivially short chatter ("ok", "done") is discarded — precision over
   recall.
2. **Classifies** each note by cheap local signals into an entity type:
   `root-cause` (a failure plus its diagnosis), `pitfall` (a failure to
   avoid — markers aligned with the `mimir_check_failure_pattern` deja-vu
   guard, #521, so captured pitfalls are findable by it), `decision`,
   `pattern`, or `takeaway`.
3. **Keys** each note with a stable slug of its summary line, and
   **remembers** it through the normal write path: `category="capture"`,
   `source="capture"`, layer `buffer`, moderate importance (0.6). Captured
   memories then live the standard lifecycle — decay, promotion,
   consolidation, recall — like any other entity.

## Flood control (by design)

- **Near-duplicate merging stays ON.** A re-captured solved problem
  (reworded, different headline) merges into the existing memory via the
  trigram dedup instead of piling up siblings. There is deliberately no
  `skip_dedup` for captures.
- **Same headline → same key → in-place update**, not a new row.
- **Hard cap per invocation** (20; callers can lower it with
  `max_entities`, never raise it). Notes beyond the cap are dropped and
  counted in the result (`dropped`), never silently eaten.
- `--dry-run` / `dry_run: true` distills and reports without writing.

## CLI usage

```bash
# From a hook or pipeline: distill stdin, write to the default vault
some-agent --dump-session | perseus-vault capture

# From a file, scoped to a workspace, preview only
perseus-vault capture --file transcript.jsonl --workspace-hash ws-myproj --dry-run

# Optional LLM distillation (falls back to the rule-based path on ANY
# LLM failure or timeout — see MIMIR_LLM_TIMEOUT_SECS, #528)
perseus-vault capture --file notes.md --llm \
  --llm-endpoint http://localhost:11434/api/generate --llm-model llama3
```

Output is a JSON report: `captured` / `created` / `updated` / `merged` /
`dropped`, the distiller used (`rule_based` or `llm`, plus `llm_fallback`
with the reason when the LLM path degraded), and a per-note breakdown
(`key`, `type`, `summary`, `action`). A failed capture exits non-zero AND
prints `{"ok": false, ...}` (#516 pattern).

## MCP usage

```json
{ "name": "perseus_vault_capture",
  "arguments": { "text": "<transcript or insight payload>",
                 "workspace_hash": "ws-myproj", "dry_run": false } }
```

Same pipeline, same report, same flood control. `llm: true` uses the
server's configured `--llm-endpoint` and degrades identically.

## Wiring it to session lifecycle hooks

Capture is the **on_insight / SessionEnd** stage of the session lifecycle
contract (see the hook contract doc from #523/#540 — SessionStart recalls,
on_insight captures, SessionStop consolidates):

- **on_insight (mid-session):** the moment something durable happens, pipe
  it in — `echo "<the insight>" | perseus-vault capture` — or call
  `perseus_vault_capture` from the agent. Near-dup merging makes repeated
  captures of the same insight harmless.
- **SessionEnd:** capture first, then run the hygiene pass — the captured
  notes land in the `buffer` layer and `maintain` immediately gets a chance
  to merge/promote them:

```json
{
  "hooks": {
    "SessionEnd": [
      { "matcher": "*",
        "hooks": [
          { "type": "command",
            "command": "cat \"$CLAUDE_TRANSCRIPT_PATH\" | perseus-vault capture --dry-run",
            "timeout": 60 },
          { "type": "command",
            "command": "perseus-vault maintain",
            "timeout": 120 }
        ] }
    ]
  }
}
```

(Drop `--dry-run` once you've previewed what your transcripts distill
into. Order matters: **capture, then `maintain`** — end-of-session
semantics are "persist what was learned, then groom".)

## The LLM path is optional, the rule-based path is the floor

The default distiller is pure, deterministic Rust — the same air-gapped
bar as `mimir_extract` (#234). With `llm: true` / `--llm`, the configured
endpoint is asked to distill instead (strict-JSON contract; unknown entity
types degrade to `takeaway` — model output is untrusted). On **any** LLM
failure — endpoint not configured, transport error, timeout
(`MIMIR_LLM_TIMEOUT_SECS`, default 30s), or unparseable output — the
pipeline falls back to the rule-based distiller and says so in the
report's `llm_fallback` field. A capture invocation never comes back
empty-handed because a model was slow, down, or chatty.
