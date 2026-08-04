# Signed, distributable policy/authority profiles

Status: implementation contract
Date: 2026-08-04
Resolves: #837
Related: [Structured-truth retrieval policy](specs/structured-truth-retrieval-policy.md) ·
AAR control plane (#768) ·
release-provenance CI: [SLSA build provenance](https://github.com/Perseus-Computing-LLC/perseus-vault/pull/569) ·
[reproducible signed release builds](https://github.com/Perseus-Computing-LLC/perseus-vault/pull/716)

## What this is

Authority manifests and policy bundles are now distributable artifacts with
sigstore-style **content attestation**: a profile is canonical JSON carrying
an authority-manifest payload, the signer's Ed25519 public key, and a
signature over the canonicalized payload. On load the profile is verified
before the manifest takes effect; **verification failure = no authority**
(fail closed). The verification result — signer identity fingerprint, payload
digest, outcome — is recorded in the ledger journal (`profile_load` events).

## Relationship to release provenance

Release provenance (SLSA attestations on release artifacts, #569/#716)
proves *how an artifact was built*. Profile signing proves *who authorized
the content of a policy/authority bundle* — the two compose: SLSA covers the
binary's supply chain; profile attestation covers the policy that governs
what the binary may authorize. Profile signing is **not** a replacement for
release provenance, and the release-provenance CI is **not** a profile
signer: profiles are signed by the authority operator with the trusted key
and verified against that key on load.

## Format

```json
{
  "schema": "perseus-policy-profile/v1",
  "signer_key_b64": "<base64 raw 32-byte Ed25519 public key>",
  "payload": { "<AuthorityManifestInput fields>" },
  "signature_b64": "<base64 Ed25519 signature over canonical(payload)>"
}
```

Canonicalization is sorted-key JSON with no insignificant whitespace
(`signed_profile::canonical_json_bytes`), so a tampered or re-ordered field
breaks verification.

## Load semantics

- `authority_set_signed(profile_json, trusted_public_key_b64, author)`:
  1. verify the signature over the canonical payload with the embedded key;
  2. **identity check**: the embedded signer must be the trusted key;
  3. on success, the payload is applied via the normal `authority_set` path
     and the verification result is journaled (`profile_load`, category
     `authority_profile` — signer fingerprint, payload digest, outcome);
  4. any failure returns `SIGNED_PROFILE_REJECTED` and grants **no**
     authority — no manifest row is written, nothing takes effect.
- MCP surface: `mimir_authority_set_signed` / `perseus_vault_authority_set_signed`.

## Scope and non-goals

- Bounded to manifest/policy loading; no claim of universal security from
  one attestation scheme.
- No key management system: the trusted public key is supplied by the
  operator at load time.
- The profile payload is the same `AuthorityManifestInput` shape as
  `authority_set`; argv-level capability constraints (#836) and resource
  constraints are carried unchanged in `capability_constraints_json`.
