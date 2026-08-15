# FIPS-Validated Cryptography Path for CUI Deployments (G2 — SC-3.13.11)

**Status:** guidance + decision · **Resolves:** #1068 (matrix gap G2) · **Date:** 2026-08-15

## 1. Requirement

NIST SP 800-171 Rev 2, **3.13.11**: *Employ FIPS-validated cryptography when used to protect the confidentiality of CUI.* (Rev 3 restates this as 03.13.11 "Cryptographic Protection" with an organization-defined assignment for cryptography types.)

## 2. Current state (honest)

| Layer | Mechanism | FIPS-validated? |
|---|---|---|
| At-rest (vault DB + artifacts) | AES-256-GCM (RustCrypto `aes-gcm`) | **No** — sound implementation, no CMVP certificate |
| Receipt integrity (Ledger) | HMAC-SHA256 + trusted-key attestations | **No** — not a certified module |
| Transport | TLS | depends on endpoint library/terminator (OpenSSL 3 FIPS provider available) |

The matrix therefore marks 3.13.11 **GAP** today. This document converts that into a dated, deployable posture.

## 3. Path A — FIPS-terminated transport (deploy now, no code change)

Terminate TLS at a FIPS-validated endpoint in the enclave: OpenSSL 3 with the FIPS provider (fips=yes), or a FIPS-certified TLS terminator/load balancer in front of the vault server. The vault's MCP/gRPC traffic rides inside the FIPS-validated channel; session keys and bulk encryption are then provided by a validated module.

- Evidence for assessor: terminator/module certificate number (CMVP #), configuration proving fips=yes, traffic path diagram.
- Cost: infrastructure config, ~days.

## 4. Path B — OS-level FIPS at rest (deploy now, no code change)

Place the vault DB volume on FIPS-validated volume encryption (LUKS/dm-crypt with a FIPS-enabled kernel crypto stack, or an equivalent validated full-volume product). The application-level AES-256-GCM remains as defense-in-depth (documented as compensating, not as the 3.13.11 control).

- Evidence: volume encryption config + validated module reference; key management procedure (key escrow/rotation) per SC-3.13.10.
- Cost: infrastructure config, ~days.

## 5. Path C — FIPS 140-3 validation of the vault crypto module (roadmap)

The RustCrypto `aes-gcm`/HMAC implementations have **no CMVP validation**. Certifying a module (CAVP algorithm testing + CMVP FIPS 140-3) is a months-long, cost-bearing program.

- Decision: **not now**. Track as a roadmap item; re-evaluate when (a) a customer requires validated in-process crypto specifically, or (b) RustCrypto or an alternative gains validation.
- Interim SSP language: Paths A+B implemented; in-process module certification listed on POA&M.

## 6. Decision

**Recommendation:** adopt Paths A+B for any CUI enclave deployment (immediate, no code change), keep Path C on the roadmap. Matrix row 3.13.11 moves from GAP to **PARTIAL (deployment path, dated plan)**. This issue closes when this doc is committed; Path C tracking lives in the roadmap.
