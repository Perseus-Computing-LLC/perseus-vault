# Live-web gap-fill (opt-in)

`perseus_vault_web_gap_fill` closes the recall-miss loop: when nothing in the
store clears the relevance bar, the AGENT (not the Vault) fetches the live
web, then reports the grounded content + source URLs back for validated,
audited storage as **unverified-until-confirmed** — so the next pull hits
memory instead of the network.

## Security posture (read before enabling)

- **The Vault never makes network calls for this feature.** No SSRF surface,
  no egress — the air-gap / federal no-telemetry posture is preserved. The
  fetch is delegated to the agent's own web tools.
- **OFF by default.** The tool errors until `PERSEUS_VAULT_WEB_GAP_FILL_ENABLED=1`.
  Recall output stays byte-identical while off (the `gap` signal is only
  emitted when the feature is on — never an implicit recall fallback).
- **Per-workspace source allowlist.** `PERSEUS_VAULT_WEB_ALLOWLIST=/path.json`:
  `{"<workspace_hash>": ["docs.example.com", ...] | "*"}`. A workspace absent
  from the file (and no `"*"` key) is denied. A host entry `"*"` allows any
  host for that workspace — the operator's explicit choice.
- **http/https only**; URLs with userinfo are rejected; literal IPs in
  private/reserved ranges (loopback, RFC1918, link-local, metadata
  `169.254.169.254`, multicast, documentation ranges, IPv6 equivalents) are
  refused even when allowlisted.
- **Fail-closed secret scan.** Content matching a known secret class
  (OpenAI/GitHub/AWS/Slack/Google keys, OAuth tokens, `PRIVATE KEY` blocks,
  `Bearer`/`Authorization` headers, JWTs) is refused with the class name.
- **Relevance floor.** `relevance_score` (agent-judged, 0-1) must clear
  `PERSEUS_VAULT_WEB_MIN_RELEVANCE` (default 0.6) or the write is refused.
- **Rate limit.** `PERSEUS_VAULT_WEB_RATE_LIMIT` (default 10) caps writes per
  workspace per hour (fixed window, state-store backed).
- **Never auto-promoted.** Writes carry `"verification":
  "unverified_until_confirmed"` in the body and pass through the normal
  audited remember path (origin: `web_gap_fill` / `agent_fetch`); promotion
  to verified requires an explicit operator action.

## Usage

```json
{
  "query": "perseus vault scheduled eval",
  "content": "…page text the agent actually fetched…",
  "title": "Docs page title",
  "sources": ["https://docs.example.com/guide"],
  "workspace_hash": "<ws>",
  "relevance_score": 0.9
}
```

Defaults: `category` = `web`; `key` = `web-<sha256(content)[..16]>`
(re-fetching the same bytes updates the same entity — natural dedup). Max
content 64 KiB, max 8 sources, title/query bounded.

## Recall gap signal

With the feature enabled, an empty recall adds `"gap": true` plus a
`gap_fill` hint to its response. This is a signal to the agent, not a
fallback — the agent decides whether to fetch, and the write still has to
pass every gate above.

## Config summary

| Env | Default | Meaning |
|---|---|---|
| `PERSEUS_VAULT_WEB_GAP_FILL_ENABLED` | unset (off) | `1` opts in |
| `PERSEUS_VAULT_WEB_ALLOWLIST` | unset (all denied) | path to workspace→hosts JSON |
| `PERSEUS_VAULT_WEB_RATE_LIMIT` | 10 | writes/workspace/hour |
| `PERSEUS_VAULT_WEB_MIN_RELEVANCE` | 0.6 | relevance floor |
