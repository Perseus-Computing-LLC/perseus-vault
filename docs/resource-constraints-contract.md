# Resource-bound Authorized Action constraints

Cross-repository contract for Hermes Vault plugin issue #10.

`authority_set` accepts optional capability-bound constraint JSON and
`action_intent` accepts optional action constraint JSON. The host canonicalizes
and SHA-256 hashes these fields. Durable lifecycle records contain the
version/hash projection; raw prompts, credentials, card data, and tool
arguments are never persisted.

Supported bounds include exact repository/environment/destination/merchant
references, non-increasing `amount_minor`, exact three-letter currency, and a
non-extendable timezone-aware `expires_at`. Checks occur before execution and
approval/lease transitions fail closed on retargeting, amount increases,
currency changes, expiry extension, expiry, or replay.

Legacy AAR records remain readable and unconstrained legacy clients remain
compatible. Capabilities that require constraints must not treat absent fields
as approval.

Related issue: https://github.com/Perseus-Computing-LLC/hermes-plugin-perseus-vault/issues/10
Ledger projection: https://github.com/Perseus-Computing-LLC/ledger/issues/183
Vault authority lifecycle: https://github.com/Perseus-Computing-LLC/perseus-vault/issues/768

Status: locally verified only until provider and Ledger PRs are landed.
