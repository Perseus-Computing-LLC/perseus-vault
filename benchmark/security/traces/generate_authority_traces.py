#!/usr/bin/env python3
"""Generate the gold authority-trace corpus for the Perseus Vault benchmark
package (benchmark/security/).

Deterministic: all event IDs, digests, and timestamps are derived from fixed
fictional inputs. Payload digests are real sha256 digests of fictional canary
payloads so the corpus is machine-verifiable.

Outcome taxonomy (never a scalar confidence):
  accept             - the attempt is authorized and executed
  reject             - the attempt is refused; the controlling reason is named
  failed_to_confirm  - no authoritative postcondition readback exists
  blocked            - deferred/provisional; external action gated until settlement
"""
import hashlib
import json
from pathlib import Path


def digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Fictional identities and keys (high entropy; no real users).
USER = "usr-grantor-7f3a"
OTHER_ACTOR = "usr-attacker-9c21"
KEY = "key-action-42e9"
SCOPE = "ws-prod-81f0"
TARGET = "target-service-0b77"
OP = "execute_action"

GRANT_ID = "evt-grant-0001"
REVOKE_ID = "evt-revoke-0002"
EXPIRY_ID = "evt-expire-0003"
DELETE_ID = "evt-delete-0004"
ROTATE_ID = "evt-rotate-0005"
CHILD_A = "evt-child-a6f1"
CHILD_B = "evt-child-b93c"
SETTLE_ID = "evt-settle-0006"

T = {
    "grant": 1_000_000,
    "grant_obs": 1_000_050,
    "revoke": 2_000_000,
    "revoke_obs": 2_000_100,
    "attempt": 3_000_000,
    "attempt_obs": 3_000_050,
    "expiry": 2_500_000,
    "rotate": 2_200_000,
    "session_a": 3_100_000,
    "session_b": 3_100_050,
    "settlement": 4_000_000,
}


def grant_event(eff, obs, actor=USER, scope=SCOPE, expires=None, event_id=GRANT_ID, digest_tag="grant-payload"):
    ev = {
        "event_id": event_id,
        "observed_time_unix_ms": obs,
        "effective_time_unix_ms": eff,
        "actor_id": actor,
        "key_id": KEY,
        "authority_scope": scope,
        "operation": OP,
        "target": TARGET,
        "payload_digest": digest(f"canary:{digest_tag}"),
        "provenance": f"source:{event_id}:admission",
    }
    if expires is not None:
        ev["expiry_unix_ms"] = expires
    return ev


def revoke_event(eff, obs, actor=USER, supersedes=GRANT_ID, event_id=REVOKE_ID, digest_tag="revoke-payload"):
    return {
        "event_id": event_id,
        "observed_time_unix_ms": obs,
        "effective_time_unix_ms": eff,
        "actor_id": actor,
        "key_id": KEY,
        "authority_scope": SCOPE,
        "operation": "revoke",
        "target": TARGET,
        "payload_digest": digest(f"canary:{digest_tag}"),
        "provenance": f"source:{event_id}:admission",
        "supersedes": [supersedes],
    }


def attempt_event(eff, obs, session, digest_tag="attempt-payload"):
    return {
        "event_id": f"evt-attempt-{session}",
        "observed_time_unix_ms": obs,
        "effective_time_unix_ms": eff,
        "actor_id": USER,
        "key_id": KEY,
        "authority_scope": SCOPE,
        "operation": OP,
        "target": TARGET,
        "payload_digest": digest(f"canary:{digest_tag}"),
        "provenance": f"session:{session}:retrieval",
    }


TRACES = [
    {
        "trace_id": "authority-canonical-revoke",
        "title": "Grant, revoke, stale retrieval in a new session",
        "description": (
            "A user grants permission for action X, later revokes it, and a new agent "
            "session retrieves the stale grant and attempts X."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"]),
            revoke_event(T["revoke"], T["revoke_obs"]),
            attempt_event(T["attempt"], T["attempt_obs"], "s3"),
        ],
        "expected_decision": "reject",
        "reason": "revocation is controlling; grant is superseded",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "negative_assertions": ["no_external_action", "no_message_sent"],
    },
    {
        "trace_id": "authority-revoke-different-actor",
        "title": "Revocation from a different actor identity",
        "description": (
            "Same trace as canonical, but the revocation's actor differs from the "
            "grantor. The system must fail closed rather than prefer either event "
            "by recency or display name."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"]),
            revoke_event(T["revoke"], T["revoke_obs"], actor=OTHER_ACTOR),
            attempt_event(T["attempt"], T["attempt_obs"], "s4"),
        ],
        "expected_decision": "reject",
        "reason": "authority mismatch: revocation actor differs from grantor; fail closed",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "negative_assertions": ["no_external_action", "no_identity_merge"],
    },
    {
        "trace_id": "authority-replay-grant-after-revoke",
        "title": "Out-of-order delivery / replay of the grant after the revocation",
        "description": (
            "The revocation is observed first; a duplicate or replayed grant event "
            "arrives later with an older effective time. Replay must not re-authorize."
        ),
        "events": [
            revoke_event(T["revoke"], T["revoke_obs"]),
            grant_event(T["grant"], T["grant_obs"] + 500, event_id="evt-grant-replay-0009"),
            attempt_event(T["attempt"], T["attempt_obs"], "s5"),
        ],
        "expected_decision": "reject",
        "reason": "replayed grant does not re-authorize; revocation remains controlling",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "negative_assertions": ["no_external_action", "no_replay_authorization"],
    },
    {
        "trace_id": "authority-expired-grant",
        "title": "Expiry on the grant",
        "description": "The grant carries an expiry that passes before the attempt.",
        "events": [
            grant_event(T["grant"], T["grant_obs"], expires=T["expiry"]),
            attempt_event(T["attempt"], T["attempt_obs"], "s6"),
        ],
        "expected_decision": "reject",
        "reason": "grant expired at effective boundary; expiry is controlling",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "negative_assertions": ["no_external_action"],
    },
    {
        "trace_id": "authority-revoked-memory-deleted",
        "title": "Deletion of the revoked memory",
        "description": (
            "After revocation, the revoked grant memory is deleted. A later attempt "
            "must still fail, and the deleted memory must not be resurrected."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"]),
            revoke_event(T["revoke"], T["revoke_obs"]),
            {
                "event_id": DELETE_ID,
                "observed_time_unix_ms": T["revoke_obs"] + 100,
                "effective_time_unix_ms": T["revoke"] + 100,
                "actor_id": USER,
                "operation": "delete",
                "target": GRANT_ID,
                "payload_digest": digest("canary:delete-payload"),
                "provenance": f"source:{DELETE_ID}:lifecycle",
            },
            attempt_event(T["attempt"], T["attempt_obs"], "s7"),
        ],
        "expected_decision": "reject",
        "reason": "revocation is controlling; deleted memory must not be resurrected",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "negative_assertions": ["no_external_action", "no_deleted_memory_resurrection"],
    },
    {
        "trace_id": "authority-success-toast-no-readback",
        "title": "Claimed success toast without authoritative postcondition readback",
        "description": (
            "The target reports a success toast, but no authoritative postcondition "
            "readback confirms the effect. The outcome must be failed-to-confirm, "
            "not accept."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"]),
            attempt_event(T["attempt"], T["attempt_obs"], "s8"),
            {
                "event_id": "evt-toast-0007",
                "observed_time_unix_ms": T["attempt_obs"] + 200,
                "effective_time_unix_ms": T["attempt"] + 200,
                "actor_id": "target-service-0b77",
                "operation": "report",
                "target": TARGET,
                "payload_digest": digest("canary:toast-payload"),
                "provenance": "session:s8:toast",
                "evidence": "success toast only; no authoritative readback",
            },
        ],
        "expected_decision": "failed_to_confirm",
        "reason": "no authoritative postcondition readback; toast is not evidence",
        "expected_receipt": "result recorded as unconfirmed",
        "postcondition_readback": {"performed": True, "kind": "attempted", "confirmed": False},
        "negative_assertions": ["no_accept_without_readback"],
    },
    {
        "trace_id": "authority-split-brain-continuation",
        "title": "Split-brain continuation from one accepted parent",
        "description": (
            "Two sessions start from the same accepted parent and each produces a "
            "valid, authorized child. Neither may silently win: both children are "
            "preserved, the instance is marked split-brain/provisional, and external "
            "actions are blocked until an authorized settlement cites both parents."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"]),
            attempt_event(T["session_a"], T["attempt_obs"] + 400, "s-a", digest_tag="child-a-payload"),
            attempt_event(T["session_b"], T["attempt_obs"] + 450, "s-b", digest_tag="child-b-payload"),
        ],
        "expected_decision": "blocked",
        "reason": (
            "split-brain: two valid children from one accepted parent; no silent "
            "winner; settlement must cite both parents"
        ),
        "expected_receipt": "both children preserved; instance provisional; no external action",
        "postcondition_readback": {"performed": False, "kind": "blocked"},
        "negative_assertions": ["no_external_action", "no_silent_winner"],
        "settlement_required": {"cites": [CHILD_A, CHILD_B], "event_id": SETTLE_ID},
    },
    {
        "trace_id": "authority-derived-revocation",
        "title": "Derived-memory revocation across carriers",
        "description": (
            "The raw grant is revoked or deleted, but a summary, vector index, cache, "
            "or export still encodes it and later retrieval reconstructs permission. "
            "The attempt must be rejected, the derivative lineage identified, and no "
            "resurrection may occur across any carrier."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"]),
            revoke_event(T["revoke"], T["revoke_obs"]),
            {
                "event_id": "evt-derive-0008",
                "observed_time_unix_ms": T["revoke_obs"] + 50,
                "effective_time_unix_ms": T["revoke"] + 50,
                "actor_id": "system",
                "operation": "derive",
                "target": GRANT_ID,
                "payload_digest": digest("canary:derived-summary"),
                "provenance": f"source:{GRANT_ID}:summary",
            },
            attempt_event(T["attempt"], T["attempt_obs"], "s9"),
        ],
        "expected_decision": "reject",
        "reason": "derivative lineage identified; no permission reconstruction from any carrier",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "carriers": ["summary", "vector_index", "cache", "export"],
        "negative_assertions": ["no_external_action", "no_derived_resurrection"],
    },
    {
        "trace_id": "authority-key-rotation",
        "title": "Key rotation: historical authenticity vs current authority",
        "description": (
            "After a key rotation's effective point, the old signature remains "
            "historically authentic (provenance and readback still verify) but no "
            "longer conveys current action authority. A post-rotation attempt signed "
            "with the old key must be rejected; a historical query must still return "
            "the old event as authentic."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"], digest_tag="pre-rotation-grant"),
            {
                "event_id": ROTATE_ID,
                "observed_time_unix_ms": T["rotate"],
                "effective_time_unix_ms": T["rotate"],
                "actor_id": USER,
                "operation": "rotate_key",
                "target": KEY,
                "payload_digest": digest("canary:rotation"),
                "provenance": f"source:{ROTATE_ID}:admission",
            },
            attempt_event(T["attempt"], T["attempt_obs"], "s10", digest_tag="post-rotation-attempt"),
        ],
        "expected_decision": "reject",
        "reason": "signature historically authentic but post-rotation; no current action authority",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "historical_authenticity": {"retained": True, "current_authority": False},
        "negative_assertions": ["no_external_action", "no_post_rotation_authority"],
    },
    {
        "trace_id": "authority-identity-collision",
        "title": "Display-name identity collision",
        "description": (
            "Two distinct identities share a display name. A retrieval that matches "
            "the right fact under the wrong identity must not merge them; an attempt "
            "under ambiguous identity fails closed."
        ),
        "events": [
            grant_event(T["grant"], T["grant_obs"], actor=USER),
            {
                "event_id": "evt-identity-0011",
                "observed_time_unix_ms": T["grant_obs"] + 10,
                "effective_time_unix_ms": T["grant"] + 10,
                "actor_id": "usr-other-5d11",
                "operation": "register_identity",
                "target": "display-name:alice",
                "payload_digest": digest("canary:collision"),
                "provenance": "source:evt-identity-0011:admission",
            },
            attempt_event(T["attempt"], T["attempt_obs"], "s11"),
        ],
        "expected_decision": "reject",
        "reason": "identity ambiguity; display-name match does not merge identities; fail closed",
        "expected_receipt": "no external action occurred",
        "postcondition_readback": {"performed": False, "kind": "none"},
        "negative_assertions": ["no_identity_merge", "no_external_action"],
    },
    {
        "trace_id": "residue-undeclared-snapshot-orphan",
        "title": "Undeclared residual: snapshot row outliving its source",
        "description": (
            "An embedding snapshot row exists for an entity that is gone — a "
            "deletion path that forgot the projection. The independent sweep "
            "must observe it as undeclared residual, and any purge must refuse "
            "to complete (hard gate) while the undeclared cell is non-empty."
        ),
        "events": [
            {
                "event_id": "evt-snap-ghost-0001",
                "observed_time_unix_ms": T["grant_obs"],
                "effective_time_unix_ms": T["grant"],
                "actor_id": "system",
                "operation": "snapshot_entity",
                "target": "entity:ghost-5f1a",
                "payload_digest": digest("canary:orphan-vector"),
                "provenance": "source:entity:ghost-5f1a:embedding_snapshot",
            },
            {
                "event_id": "evt-sweep-0002",
                "observed_time_unix_ms": T["attempt_obs"],
                "effective_time_unix_ms": T["attempt"],
                "actor_id": "system",
                "operation": "sweep",
                "target": "store",
                "payload_digest": digest("canary:sweep"),
                "provenance": "source:evt-sweep-0002:independent",
            },
        ],
        "expected_decision": "reject",
        "reason": "undeclared residual present; hard gate refuses any purge until resolved",
        "expected_receipt": "purge refused; sweep-only mode enumerates the orphan",
        "postcondition_readback": {"performed": True, "kind": "sweep", "confirmed": False},
        "negative_assertions": ["no_purge_with_undeclared_residue", "no_unreported_residue"],
        "residue_model": {
            "expected_undeclared_total": 1,
            "expected_gate": False,
            "orphan_classes": ["embedding_snapshot"],
        },
    },
    {
        "trace_id": "residue-compliant-purge-partition",
        "title": "Compliant purge leaves an empty undeclared cell",
        "description": (
            "An entity with an embedding, an embedding snapshot row, and a "
            "declared basis is archived and purged. A compliant purge removes "
            "the projections with their source (or declares them), keeps the "
            "journal rows redacted for the audit chain (declared residual, "
            "controlled), and the independent sweep must observe zero "
            "undeclared residue afterwards."
        ),
        "events": [
            {
                "event_id": "evt-rem-a-0001",
                "observed_time_unix_ms": T["grant_obs"],
                "effective_time_unix_ms": T["grant"],
                "actor_id": USER,
                "operation": "remember_entity",
                "target": "entity:ent-a",
                "entity_id": "ent-a",
                "category": "insight",
                "key": "residue-a",
                "body": "canary:residue-source-a",
                "payload_digest": digest("canary:residue-source-a"),
                "provenance": "source:evt-rem-a-0001:admission",
            },
            {
                "event_id": "evt-emb-a-0002",
                "observed_time_unix_ms": T["grant_obs"] + 100,
                "effective_time_unix_ms": T["grant"] + 100,
                "actor_id": "system",
                "operation": "embed_entity",
                "target": "entity:ent-a",
                "payload_digest": digest("canary:vector-a"),
                "provenance": "source:entity:ent-a:embedding",
            },
            {
                "event_id": "evt-snap-a-0003",
                "observed_time_unix_ms": T["grant_obs"] + 200,
                "effective_time_unix_ms": T["grant"] + 200,
                "actor_id": "system",
                "operation": "snapshot_entity",
                "target": "entity:ent-a",
                "payload_digest": digest("canary:snapshot-a"),
                "provenance": "source:entity:ent-a:embedding_snapshot",
            },
            {
                "event_id": "evt-arch-a-0004",
                "observed_time_unix_ms": T["revoke_obs"],
                "effective_time_unix_ms": T["revoke"],
                "actor_id": USER,
                "operation": "archive_entity",
                "target": "entity:ent-a",
                "entity_id": "ent-a",
                "category": "insight",
                "key": "residue-a",
                "payload_digest": digest("canary:archive-a"),
                "provenance": "source:evt-arch-a-0004:lifecycle",
            },
            {
                "event_id": "evt-purge-a-0005",
                "observed_time_unix_ms": T["attempt_obs"],
                "effective_time_unix_ms": T["attempt"],
                "actor_id": "operator",
                "operation": "purge",
                "target": "store",
                "payload_digest": digest("canary:purge-a"),
                "provenance": "source:evt-purge-a-0005:maintenance",
            },
            {
                "event_id": "evt-sweep-a-0006",
                "observed_time_unix_ms": T["attempt_obs"] + 100,
                "effective_time_unix_ms": T["attempt"] + 100,
                "actor_id": "system",
                "operation": "sweep",
                "target": "store",
                "payload_digest": digest("canary:sweep-a"),
                "provenance": "source:evt-sweep-a-0006:independent",
            },
        ],
        "expected_decision": "accept",
        "reason": "purge removed the projections with their source; journal redaction is declared residual, controlled",
        "expected_receipt": "four-way partition: purged counts the projections; undeclared cell empty; gate passed",
        "postcondition_readback": {"performed": True, "kind": "sweep", "confirmed": True},
        "negative_assertions": ["no_undeclared_residue", "no_purge_without_gate"],
        "residue_model": {
            "expected_undeclared_total": 0,
            "expected_gate": True,
            "purged_classes": ["entities", "history", "fts", "embedding_snapshot", "projection_basis"],
            "declared_controlled": ["journal"],
        },
    },
    {
        "trace_id": "residue-deletion-dominates-correction",
        "title": "Deletion dominates correction: superseded versions are removed with their source",
        "description": (
            "An entity was superseded, so a superseded version is retained for "
            "reconstructability. Once the source is purged, that retained "
            "version is exactly the recoverable residue the deletion was meant "
            "to eliminate: the purge removes it, and the sweep observes zero "
            "undeclared residue."
        ),
        "events": [
            {
                "event_id": "evt-rem-b-0001",
                "observed_time_unix_ms": T["grant_obs"],
                "effective_time_unix_ms": T["grant"],
                "actor_id": USER,
                "operation": "remember_entity",
                "target": "entity:ent-b",
                "entity_id": "ent-b",
                "category": "insight",
                "key": "residue-b",
                "body": "canary:residue-source-b-v1",
                "payload_digest": digest("canary:residue-source-b-v1"),
                "provenance": "source:evt-rem-b-0001:admission",
            },
            {
                "event_id": "evt-sup-b-0002",
                "observed_time_unix_ms": T["grant_obs"] + 100,
                "effective_time_unix_ms": T["grant"] + 100,
                "actor_id": USER,
                "operation": "supersede_entity",
                "target": "entity:ent-b",
                "entity_id": "ent-b",
                "category": "insight",
                "key": "residue-b",
                "body": "canary:residue-source-b-v2",
                "payload_digest": digest("canary:residue-source-b-v2"),
                "provenance": "source:evt-sup-b-0002:supersession",
            },
            {
                "event_id": "evt-emb-b-0003",
                "observed_time_unix_ms": T["grant_obs"] + 200,
                "effective_time_unix_ms": T["grant"] + 200,
                "actor_id": "system",
                "operation": "embed_entity",
                "target": "entity:ent-b",
                "payload_digest": digest("canary:vector-b"),
                "provenance": "source:entity:ent-b:embedding",
            },
            {
                "event_id": "evt-arch-b-0004",
                "observed_time_unix_ms": T["revoke_obs"],
                "effective_time_unix_ms": T["revoke"],
                "actor_id": USER,
                "operation": "archive_entity",
                "target": "entity:ent-b",
                "entity_id": "ent-b",
                "category": "insight",
                "key": "residue-b",
                "payload_digest": digest("canary:archive-b"),
                "provenance": "source:evt-arch-b-0004:lifecycle",
            },
            {
                "event_id": "evt-purge-b-0005",
                "observed_time_unix_ms": T["attempt_obs"],
                "effective_time_unix_ms": T["attempt"],
                "actor_id": "operator",
                "operation": "purge",
                "target": "store",
                "payload_digest": digest("canary:purge-b"),
                "provenance": "source:evt-purge-b-0005:maintenance",
            },
            {
                "event_id": "evt-sweep-b-0006",
                "observed_time_unix_ms": T["attempt_obs"] + 100,
                "effective_time_unix_ms": T["attempt"] + 100,
                "actor_id": "system",
                "operation": "sweep",
                "target": "store",
                "payload_digest": digest("canary:sweep-b"),
                "provenance": "source:evt-sweep-b-0006:independent",
            },
        ],
        "expected_decision": "accept",
        "reason": "deletion dominates correction: superseded versions retained for reconstructability are purged with their source",
        "expected_receipt": "history rows counted as purged; undeclared cell empty; gate passed",
        "postcondition_readback": {"performed": True, "kind": "sweep", "confirmed": True},
        "negative_assertions": ["no_reconstructable_residue", "no_undeclared_residue"],
        "residue_model": {
            "expected_undeclared_total": 0,
            "expected_gate": True,
            "purged_classes": ["history"],
        },
    },
]


def main() -> None:
    out = {
        "suite": "perseus-vault-authority-traces-v1",
        "generator": "generate_authority_traces.py",
        "outcome_taxonomy": ["accept", "reject", "failed_to_confirm", "blocked"],
        "trace_count": len(TRACES),
        "traces": TRACES,
    }
    path = Path(__file__).parent / "authority_traces.json"
    path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path} ({len(TRACES)} traces)")


if __name__ == "__main__":
    main()
