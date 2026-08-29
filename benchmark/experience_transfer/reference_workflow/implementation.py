"""Hash-only reference workflow over the shared synthetic agent view."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

try:
    from ..adapters import GovernedVaultAdapter
    from ..common import reject_forbidden, validate_corpus
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from adapters import GovernedVaultAdapter
    from common import reject_forbidden, validate_corpus

WORKFLOW_SCHEMA = "perseus-reference-workflow-receipt/v1"
WORKFLOW_ID = "attio-record-memory-governed-reuse-v1"


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _context_selection(view: Mapping[str, Any]) -> dict[str, Any]:
    """Select IDs only; no memory body is placed in the receipt."""
    return {
        "stage": "context_selection",
        "scope": view["controls"]["workspace_scope"],
        "task_id": view["task_b"]["task_id"],
        "world_state_hash": view["world"]["state_hash"],
        "candidate_memory_ids": [item["memory_id"] for item in view["experiences"]],
        "selection_digest": _sha({
            "scope": view["controls"]["workspace_scope"],
            "task_id": view["task_b"]["task_id"],
            "world_state_hash": view["world"]["state_hash"],
            "candidate_memory_ids": [item["memory_id"] for item in view["experiences"]],
        }),
    }


def _provenance(view: Mapping[str, Any]) -> dict[str, Any]:
    refs = []
    evidence_commitments = []
    source_commitments = []
    for item in view["experiences"]:
        refs.append({
            "experience_id": item["experience_id"],
            "memory_id": item["memory_id"],
            "validity_status": item["validity_status"],
            "world_state_hash": item["world_state_hash"],
            "source_id": item["source"]["source_id"],
            "source_commitment": item["source"]["commitment"],
            "evidence_ids": [evidence["evidence_id"] for evidence in item["evidence"]],
            "evidence_commitments": [evidence["commitment"] for evidence in item["evidence"]],
            "lineage_commitment": item["lineage"]["commitment"],
        })
        source_commitments.extend(item["source"]["commitment"] for _ in [0])
        evidence_commitments.extend(evidence["commitment"] for evidence in item["evidence"])
    return {
        "stage": "provenance",
        "references": refs,
        "source_commitments": source_commitments,
        "evidence_commitments": evidence_commitments,
        "provenance_digest": _sha(refs),
    }


def _authority(view: Mapping[str, Any]) -> dict[str, Any]:
    auth = view["authority"]
    return {
        "stage": "authority",
        "authority_id": auth["authority_id"],
        "captured_version": auth["captured_version"],
        "current_version": auth["current_version"],
        "status": auth["status"],
        "permitted_actions": list(auth["permitted_actions"]),
        "authority_commitment": auth["commitment"],
        "action_requested": "reuse_experience",
        "action_permitted": "reuse_experience" in auth["permitted_actions"],
    }


def build_receipt(case: Mapping[str, Any]) -> dict[str, Any]:
    view = case["agent_view"]
    selection = _context_selection(view)
    provenance = _provenance(view)
    authority = _authority(view)
    result = GovernedVaultAdapter().evaluate(view)
    if result["decision"] == "reuse":
        outcome = "answer_from_verified_experience"
    elif result["decision"] == "reject":
        outcome = "reject_stale_or_failed_experience"
    elif result["decision"] == "abstain":
        outcome = "abstain_without_sufficient_evidence"
    else:
        outcome = "block_before_action"
    stages = [selection, provenance, authority, {
        "stage": "decision",
        "decision": result["decision"],
        "reason_code": result["reason_code"],
        "outcome": outcome,
        "revalidated": result["revalidated"],
    }]
    receipt = {
        "schema": WORKFLOW_SCHEMA,
        "workflow_id": WORKFLOW_ID,
        "case_id": case["case_id"],
        "workspace_scope": view["controls"]["workspace_scope"],
        "task_id": view["task_b"]["task_id"],
        "world_state_hash": view["world"]["state_hash"],
        "selected_memory_ids": selection["candidate_memory_ids"] if result["decision"] == "reuse" else [],
        "source_commitments": provenance["source_commitments"],
        "evidence_commitments": provenance["evidence_commitments"],
        "authority_commitment": authority["authority_commitment"],
        "decision": result["decision"],
        "reason_code": result["reason_code"],
        "outcome": outcome,
        "revalidated": result["revalidated"],
        "stage_digests": [{"stage": stage["stage"], "digest": _sha(stage)} for stage in stages],
        "sensitive_payload": "not_captured",
    }
    receipt["receipt_sha256"] = _sha(receipt)
    reject_forbidden(receipt)
    return receipt


def run(corpus_path: Path, out_path: Path) -> dict[str, Any]:
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    wanted = {"vet-01", "vet-04", "vet-13", "vet-17"}
    cases = [case for case in corpus["cases"] if case["case_id"] in wanted]
    if {case["case_id"] for case in cases} != wanted:
        raise ValueError("reference workflow cases missing")
    receipts = [build_receipt(case) for case in cases]
    output = {
        "schema": "perseus-reference-workflow-report/v1",
        "status": "provider_free_synthetic_reference",
        "workflow_id": WORKFLOW_ID,
        "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        "receipts": receipts,
        "provider_calls": 0,
        "external_side_effects": 0,
        "boundary": {
            "supported": ["hash-only context/memory/provenance/authority/decision receipt over synthetic fixtures"],
            "not_supported": ["live Attio deployment", "live Vault authority/AAR receipt", "agent answer quality", "customer or production efficacy"],
        },
    }
    reject_forbidden(output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(out_path), "receipts": len(receipts), "sha256": hashlib.sha256(out_path.read_bytes()).hexdigest()}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("corpus/corpus.json"))
    parser.add_argument("--out", type=Path, default=Path("reference_workflow/receipts.json"))
    args = parser.parse_args()
    print(json.dumps(run(args.corpus, args.out), sort_keys=True))
