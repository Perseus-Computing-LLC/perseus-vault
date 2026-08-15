# NIST SP 800-171 Rev 2 → Rev 3 Crosswalk + CMMC ODP Alignment (G3)

**Status:** living document · **Resolves:** #1069 (matrix gap G3) · **Date:** 2026-08-15

## 1. Why this exists

The compliance matrix is structured on **Rev 2** because that is what CMMC L2 and DFARS assess against (32 CFR 170 §170.20: "NIST SP 800-171 R2 DoD assessments"). NIST withdrew Rev 2 on 2024-05-14 and superseded it with **Rev 3** (May 2024). Assessors increasingly ask for both structures, and any forward procurement may reference Rev 3. This crosswalk keeps the Rev 2 matrix operative while documenting the forward structure.

## 2. Structural comparison

- **Family numbering is preserved** for the 14 shared families: 3.1 AC, 3.2 AT, 3.3 AU, 3.4 CM, 3.5 IA, 3.6 IR, 3.7 MA, 3.8 MP, 3.9 PS, 3.10 PE, 3.11 RA, 3.12 CA, 3.13 SC, 3.14 SI — identical family names in the same positions in both revisions.
- **Rev 3 adds three families**: 3.15 PL (Planning), 3.16 SA (System & Services Acquisition), 3.17 SR (Supply Chain Risk Management).
- **Rev 3 gives every requirement a title** and keeps withdrawn requirement numbers as "Withdrawn" markers (33 withdrawn slots across the 14 shared families: AC 6, AT 1, AU 1, CM 2, IA 4, MA 3, MP 2, PE 3, RA 1, CA 1, SC 6, SI 3).
- **Rev 3 eliminates the basic/derived distinction**, tightens language to SP 800-53 control text, and introduces organization-defined parameters (Appendix D).
- Counts: Rev 2 = 110 requirements / 14 families. Rev 3 = 67 active requirements + 33 withdrawn slots across 14 shared families + 9 new requirements across PL/SA/SR (3+3+3).

## 3. Requirement-level mapping of note (shared families)

| Rev 2 | Rev 3 | Change |
|---|---|---|
| 3.13.8 (transmission crypto) + 3.13.16 (CUI at rest) | 03.13.08 Transmission and Storage Confidentiality | **merged** |
| 3.13.11 (FIPS-validated crypto) | 03.13.11 Cryptographic Protection | restated, ODP for crypto types |
| 3.3.1 (create/retain logs) | 03.03.01 Event Logging + 03.03.02 Audit Record Content + 03.03.03 Audit Record Generation | **split + content requirement added** |
| 3.1.5 (least privilege) | 03.01.05 + 03.01.06 (privileged accounts) + 03.01.07 (privileged functions) | **split** |
| — | 03.04.10 System Component Inventory, 03.04.11 Information Location | **new** (CM) |
| — | 03.05.12 Authenticator Management | **new** (IA) |
| — | 03.14.08 Information Management and Retention | **new** (SI) |
| — | 03.10.07 Physical Access Control, 03.10.08 Access Control for Transmission | **new** (PE) |
| 3.12.x (assessment family) | 03.12.05 Information Exchange added; 3.12.4 (SSP) withdrawn (moved to 03.15.02 System Security Plan under PL) | restructured |

## 4. Organization-Defined Parameters (Rev 3 Appendix D)

26 requirement groups carry one or more ODP assignments (42 assignment lines in Appendix D). The product-relevant ones:

- **03.01.01 Account Management** — review frequency, time periods, circumstances (maps to manifest expiry/review cadence)
- **03.03.01 Event Logging** — org-defined event types + frequency (maps to journal event taxonomy + retention config)
- **03.03.04 Response to Audit Logging Process Failures** — alert time period (maps to fail-closed AAR surfacing)
- **03.12.01 Security Assessment** — org-defined frequency (maps to self-assessment cadence)
- **03.13.09 Network Disconnect**, **03.13.10 Key Establishment and Management**, **03.13.11 Cryptographic Protection** — org-defined values (maps to enclave config + FIPS path G2)
- **03.14.01 Flaw Remediation**, **03.14.02 Malicious Code Protection** — time periods/frequency
- PL/SA/SR families carry their own ODPs (out of product scope today).

**CMMC implication:** the CMMC Level 2 assessment aligns ~30 assessment objectives with Rev 3 ODP-bearing requirements, and DoD's chosen ODP values become binding for those objectives. Until 32 CFR 170 is updated to adopt Rev 3, treat the Rev 2 matrix as operative and this crosswalk as forward documentation. If/when DoD transitions CMMC to Rev 3, flip the matrix's primary structure — the artifact mappings carry over unchanged because they are product facts, not standard-version facts.

## 5. Sources

- NIST SP 800-171r3 (May 2024) — https://doi.org/10.6028/NIST.SP.800-171r3
- NIST SP 800-171r2 withdrawal notice (2024-05-14)
- 32 CFR 170 §170.20 (standards acceptance: NIST SP 800-171 R2 DoD assessments)
- Secondary: NIST IR 8477 (set-theory crosswalk method); k2grc Rev 2→3 crosswalk blog (30 CMMC L2 objectives aligned to Rev 3 ODPs)
