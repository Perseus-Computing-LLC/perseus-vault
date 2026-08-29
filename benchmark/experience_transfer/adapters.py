"""Provider-neutral adapters for the deterministic reference benchmark."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

try:  # package import when used as benchmark.adapters
    from .common import ContractError, validate_agent_view, validate_adapter_result
except ImportError:  # direct import from benchmark/ during generator use
    from common import ContractError, validate_agent_view, validate_adapter_result


class NotMeasured(RuntimeError):
    """The adapter is specified but deliberately not executed."""


@dataclass(frozen=True)
class AdapterMetadata:
    name: str
    version: str
    status: str
    reason: str


class Adapter:
    metadata: AdapterMetadata

    def evaluate(self, view: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


def _result(
    adapter: str,
    case_id: str,
    decision: str,
    reason_code: str,
    *,
    revalidated: bool = False,
    provenance_validated: bool = False,
    authority_checked: bool = False,
    unsafe_reuse: bool = False,
    selected_memory_count: int = 0,
    transition_steps: int = 0,
) -> dict[str, Any]:
    result = {
        "adapter": adapter,
        "case_id": case_id,
        "decision": decision,
        "reason_code": reason_code,
        "revalidated": revalidated,
        "provenance_validated": provenance_validated,
        "authority_checked": authority_checked,
        "unsafe_reuse": unsafe_reuse,
        "selected_memory_count": selected_memory_count,
        "transition_steps": transition_steps,
    }
    validate_adapter_result(result, case_id=case_id, adapter=adapter)
    return result


def _observed_risk_flags(view: Mapping[str, Any]) -> set[str]:
    flags: set[str] = set()
    current_hash = view["world"]["state_hash"]
    experiences = view["experiences"]
    if any(item["validity_status"] == "stale" for item in experiences):
        flags.add("stale_world")
    if any(
        item["world_state_hash"] != current_hash and item["validity_status"] != "superseded"
        for item in experiences
    ):
        flags.add("stale_world")
    current_experiences = [item for item in experiences if item["validity_status"] == "current"]
    if len(current_experiences) > 1:
        parent_sets = [set(item["lineage"]["parent_experience_ids"]) for item in current_experiences]
        if parent_sets and set.intersection(*parent_sets):
            flags.add("split_brain")
        else:
            flags.add("contradiction")
    if any(item["validity_status"] == "superseded" for item in experiences):
        flags.add("superseded")
    if any(item["lineage"]["derivation_status"] == "contaminated" for item in experiences):
        flags.add("derived_revoked_parent")
    if any(
        item["lineage"]["derivation_status"] == "derived"
        and item["lineage"]["raw_source_status"] == "unknown"
        for item in experiences
    ):
        flags.add("derived_contamination")
    if any(
        evidence["status"] in {"revoked", "deleted"}
        for item in experiences for evidence in item["evidence"]
    ):
        flags.add("revoked_evidence")
    if any(
        evidence["status"] in {"missing", "insufficient"}
        for item in experiences for evidence in item["evidence"]
    ):
        flags.add("missing_evidence")
    authority = view["authority"]
    if authority["status"] != "active" or authority["current_version"] != authority["captured_version"]:
        flags.add("authority_changed")
    if view["revalidation"]["required"]:
        flags.add("revalidation_required")
    return flags


class StatelessAdapter(Adapter):
    metadata = AdapterMetadata(
        name="stateless",
        version="reference-1",
        status="pass",
        reason="deterministic no-memory baseline; it cannot transfer an experience",
    )

    def evaluate(self, view: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(view.get("case_id", "unknown"))
        # Deliberately do not inspect experiences, labels, or world state.
        return _result(self.metadata.name, case_id, "abstain", "no_memory_available")


class UngovernedRecallAdapter(Adapter):
    metadata = AdapterMetadata(
        name="ungoverned_recall",
        version="reference-1",
        status="pass",
        reason="deterministic plain recall reference; it does not validate provenance or authority",
    )

    def evaluate(self, view: Mapping[str, Any]) -> dict[str, Any]:
        validate_agent_view(str(view.get("case_id", "unknown")), view)
        case_id = view["case_id"]
        experiences = view["experiences"]
        worked = [item for item in experiences if item["approach_outcome"] == "verified_success"]
        if worked:
            risk = bool(_observed_risk_flags(view))
            return _result(
                self.metadata.name,
                case_id,
                "reuse",
                "recall_first_worked_experience",
                unsafe_reuse=risk,
                selected_memory_count=1,
                transition_steps=4,
            )
        failed = [item for item in experiences if item["approach_outcome"] == "failed"]
        if failed:
            return _result(
                self.metadata.name,
                case_id,
                "reject",
                "recalled_failed_approach",
                selected_memory_count=1,
                transition_steps=4,
            )
        return _result(self.metadata.name, case_id, "abstain", "no_reusable_experience")


class GovernedVaultAdapter(Adapter):
    metadata = AdapterMetadata(
        name="perseus_vault_governed",
        version="reference-1",
        status="pass",
        reason="provider-free governed decision policy over the shared contract view; no Vault process or provider call",
    )

    @staticmethod
    def _evidence_sufficient(experience: Mapping[str, Any]) -> bool:
        evidence = experience.get("evidence", [])
        return bool(evidence) and all(
            item.get("status") == "verified" and item.get("quality") == "sufficient"
            for item in evidence
        )

    @staticmethod
    def _candidate(experience: Mapping[str, Any]) -> bool:
        return (
            experience.get("validity_status") == "current"
            and experience.get("approach_outcome") == "verified_success"
        )

    def evaluate(self, view: Mapping[str, Any]) -> dict[str, Any]:
        case_id = str(view.get("case_id", "unknown"))
        # This adapter refuses malformed/tampered observations instead of
        # converting them into a safe-looking abstention result.
        validate_agent_view(case_id, view)
        flags = _observed_risk_flags(view)
        authority = view["authority"]
        revalidation = view["revalidation"]
        experiences = view["experiences"]
        current_world_hash = view["world"]["state_hash"]

        if "cross_workspace" in flags:
            return _result(self.metadata.name, case_id, "block", "workspace_scope_violation", provenance_validated=True, authority_checked=True)
        if "split_brain" in flags:
            return _result(self.metadata.name, case_id, "block", "split_brain_requires_settlement", provenance_validated=True, authority_checked=True)
        if "contradiction" in flags:
            return _result(self.metadata.name, case_id, "block", "unresolved_contradiction", provenance_validated=True, authority_checked=True)
        if authority["status"] != "active" or authority["current_version"] != authority["captured_version"]:
            reason = "authority_rotated" if authority["status"] == "rotated" else "authority_revoked"
            return _result(self.metadata.name, case_id, "block", reason, provenance_validated=True, authority_checked=True)

        # Derived state whose raw parent has been revoked/deleted cannot be
        # promoted by a later summary or cache. Unknown derivation is not
        # treated as valid direct evidence.
        for experience in experiences:
            lineage = experience["lineage"]
            if lineage["derivation_status"] == "contaminated" and lineage["raw_source_status"] in {"revoked", "deleted"}:
                return _result(
                    self.metadata.name, case_id, "reject", "derived_lineage_revoked",
                    provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
                )
        if any(
            experience["lineage"]["derivation_status"] == "derived"
            and experience["lineage"]["raw_source_status"] == "unknown"
            for experience in experiences
        ):
            return _result(
                self.metadata.name, case_id, "abstain", "derived_lineage_unknown",
                provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
            )

        if "missing_evidence" in flags or "revoked_evidence" in flags:
            status = experiences[0]["evidence"][0]["status"]
            reason = {
                "revoked": "evidence_revoked",
                "deleted": "evidence_deleted",
                "missing": "evidence_missing",
                "insufficient": "evidence_insufficient",
            }.get(status, "evidence_not_usable")
            return _result(
                self.metadata.name, case_id, "abstain", reason,
                provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
            )

        # Stale world state is reject-by-default. Only a performed, passing
        # revalidation bound to the current state can reopen reuse.
        revalidated = False
        if "stale_world" in flags or any(item["validity_status"] == "stale" for item in experiences):
            if not revalidation["required"]:
                return _result(
                    self.metadata.name, case_id, "reject", "stale_world_state",
                    provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
                )
            if revalidation["result"] != "pass" or not revalidation["performed"] or revalidation["current_world_state_hash"] != current_world_hash:
                return _result(
                    self.metadata.name, case_id, "reject", "revalidation_failed",
                    provenance_validated=True, authority_checked=True, revalidated=revalidation["performed"], selected_memory_count=1, transition_steps=5,
                )
            revalidated = True

        candidates = [experience for experience in experiences if self._candidate(experience)]
        if revalidated:
            candidates = [
                experience for experience in experiences
                if experience["approach_outcome"] == "verified_success"
                and experience["validity_status"] in {"stale", "current"}
            ]
        if "superseded" in flags and not candidates:
            return _result(
                self.metadata.name, case_id, "reject", "superseding_evidence_missing",
                provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
            )
        if any(experience["approach_outcome"] == "failed" for experience in experiences) and not candidates:
            return _result(
                self.metadata.name, case_id, "reject", "failed_approach_avoidance",
                provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
            )

        if not candidates:
            return _result(
                self.metadata.name, case_id, "abstain", "no_current_verified_experience",
                provenance_validated=True, authority_checked=True, selected_memory_count=0, transition_steps=4,
            )
        chosen = candidates[0]
        if not self._evidence_sufficient(chosen):
            return _result(
                self.metadata.name, case_id, "abstain", "evidence_not_sufficient",
                provenance_validated=True, authority_checked=True, selected_memory_count=1, transition_steps=4,
            )
        return _result(
            self.metadata.name,
            case_id,
            "reuse",
            "revalidated_current_world" if revalidated else (
                "current_superseding_experience" if "superseded" in flags else "verified_current_experience"
            ),
            revalidated=revalidated,
            provenance_validated=True,
            authority_checked=True,
            unsafe_reuse=False,
            selected_memory_count=1,
            transition_steps=5 if revalidated else 4,
        )


class ExternalImplementationAdapterSpec(Adapter):
    metadata = AdapterMetadata(
        name="external_implementation",
        version="contract-only-1",
        status="not_measured",
        reason="external implementation adapter is specified for a separately authorized cross-implementation run; no external code or provider was executed",
    )

    def evaluate(self, view: Mapping[str, Any]) -> dict[str, Any]:
        raise NotMeasured(self.metadata.reason)


EXECUTABLE_ADAPTERS = (StatelessAdapter(), UngovernedRecallAdapter(), GovernedVaultAdapter())
ALL_ADAPTER_METADATA = tuple(adapter.metadata for adapter in EXECUTABLE_ADAPTERS) + (ExternalImplementationAdapterSpec().metadata,)
