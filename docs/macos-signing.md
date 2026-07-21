# macOS signing and notarization

Tracks: #732 (Claude Desktop on macOS rejects the ad-hoc signed extension binary)

## The problem

Release binaries (both the `perseus-vault-aarch64-apple-darwin` tarball and
the darwin binary inside `perseus-vault.mcpb`) were historically **ad-hoc
signed** — no Apple Developer ID, no notarization:

```
$ codesign -dv perseus-vault   →  Signature=adhoc, TeamIdentifier=not set
$ spctl -a -v perseus-vault    →  rejected, source=no usable signature
```

Such a binary *executes* fine from a terminal, but Claude Desktop's extension
host applies a stricter trust check and reports "Could not connect to MCP
server" immediately, with no crash report and no logs.

## The fix (CI)

Both workflows that ship macOS binaries now sign and notarize them **when the
Apple credentials are configured**:

- `.github/workflows/release.yml` — the macOS full-binary leg builds with
  plain cargo (instead of the fused taiki-e action, which offers no signing
  hook), then runs `scripts/sign-macos.sh`, then uploads the tarball.
- `.github/workflows/mcpb.yml` — the darwin leg signs the lipo'd universal
  binary before the bundle is staged.

`scripts/sign-macos.sh` imports a Developer ID certificate into an ephemeral
keychain, signs with the hardened runtime + secure timestamp, and submits to
Apple's notary service with an App Store Connect API key. Bare Mach-O binaries
cannot be *stapled*; the notarization ticket lives in Apple's service and
Gatekeeper fetches it online at first launch.

Signing is gated on the repo **variable** `APPLE_SIGNING_ENABLED=true`. Until
the secrets below exist, CI ships ad-hoc builds exactly as before (with a
workflow warning), so nothing breaks in the meantime.

## Maintainer setup (one-time)

Prerequisite: an active Apple Developer Program membership for Perseus
Computing LLC.

1. **Create the certificate.** In Xcode or on developer.apple.com, create a
   *Developer ID Application* certificate. Export it with its private key as
   a `.p12` (Keychain Access → export), with a password.
2. **Create a notarization key.** App Store Connect → Users and Access →
   Integrations → App Store Connect API → Team Keys. Download the `.p8` and
   note the Key ID and Issuer UUID.
3. **Add repo secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `APPLE_CERTIFICATE` | `base64 -i cert.p12` output |
   | `APPLE_CERTIFICATE_PASSWORD` | the .p12 export password |
   | `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Perseus Computing LLC (ABCDE12345)` |
   | `APPLE_TEAM_ID` | 10-char team ID |
   | `APPLE_NOTARY_KEY` | `base64 -i AuthKey_XXXX.p8` output |
   | `APPLE_NOTARY_KEY_ID` | App Store Connect key ID |
   | `APPLE_NOTARY_ISSUER` | App Store Connect issuer UUID |

4. **Set the repo variable** `APPLE_SIGNING_ENABLED=true`.
5. Cut the next release tag; verify with:

   ```
   codesign -dv perseus-vault   →  TeamIdentifier=ABCDE12345
   spctl -a -t exec -vv perseus-vault  →  accepted, source=Notarized Developer ID
   ```

## User-side workarounds (until signed releases ship)

If you are affected by #732 today, either of these bypasses the unsigned
extension binary:

1. **Run perseus-vault as a plain stdio MCP server instead of a bundled
   extension.** Direct execution is unaffected by the trust check. In
   `claude_desktop_config.json`:
   ```json
   { "mcpServers": { "perseus-vault": { "command": "perseus-vault", "args": ["serve"] } } }
   ```
2. **Build from source** (`cargo build --release`) — a locally built binary
   is trusted because you built it.

Removing quarantine attributes or re-signing ad-hoc does **not** fix the
extension-host failure; the signature must be a real Developer ID.
