#!/usr/bin/env python3
"""Provider-free receipt-conditioned evidence intervention (#1136)."""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import re
from pathlib import Path
from typing import Any

FIXTURE_SCHEMA = "perseus-vault-receipt-intervention-fixture/v1"
REPORT_SCHEMA = "perseus-vault-receipt-intervention-report/v1"
ARM_IDS = ("baseline", "matched-size-control", "random-control", "receipt-blocked")
INTERVENTION_IDS = ARM_IDS[1:]
CANDIDATE_FIELDS = {
    "candidate_id",
    "source_group",
    "source_ref",
    "lane",
    "workspace_hash",
    "agent_id",
    "valid_from_unix_ms",
    "valid_to_unix_ms",
    "lifecycle",
    "token_count",
    "rank",
    "available",
}
CASE_FIELDS = {
    "case_id",
    "question",
    "workspace_hash",
    "agent_id",
    "as_of_unix_ms",
    "candidate_ids",
    "receipt_candidate_ids",
    "evaluator",
}
CONFIG_FIELDS = {
    "retrieval_mode",
    "top_k",
    "context_token_budget",
    "scan_budget",
    "reader",
    "judge",
    "seed",
}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """Raised when the fixture, receipt, intervention, or report fails closed."""


def stable_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ContractError(f"{field} must be a bounded identifier")
    return value


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{field} must be a positive integer")
    return value


def _valid_at(candidate: dict[str, Any], timestamp: int) -> bool:
    end = candidate["valid_to_unix_ms"]
    return candidate["valid_from_unix_ms"] <= timestamp and (
        end is None or timestamp < end
    )


def _normalize_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(fixture)
    normalized["candidates"] = sorted(
        normalized.get("candidates", []),
        key=lambda item: str(item.get("candidate_id", "")),
    )
    normalized["cases"] = sorted(
        normalized.get("cases", []), key=lambda item: str(item.get("case_id", ""))
    )
    for case in normalized["cases"]:
        for field in ("candidate_ids", "receipt_candidate_ids"):
            if isinstance(case.get(field), list):
                case[field] = sorted(case[field])
        evaluator = case.get("evaluator")
        if isinstance(evaluator, dict) and isinstance(
            evaluator.get("required_source_groups"), list
        ):
            evaluator["required_source_groups"] = sorted(
                evaluator["required_source_groups"]
            )
    return normalized


def _candidate_eligibility(
    candidate: dict[str, Any], *, workspace: str, agent_id: str, as_of: int
) -> str | None:
    if not candidate["available"]:
        return "unavailable"
    if candidate["workspace_hash"] != workspace:
        return "filtered_workspace"
    if candidate["agent_id"] != agent_id:
        return "filtered_agent"
    if candidate["lifecycle"] != "active":
        return f"filtered_{candidate['lifecycle']}"
    if not _valid_at(candidate, as_of):
        return "filtered_as_of"
    return None


def _validate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fixture, dict) or fixture.get("schema_version") != FIXTURE_SCHEMA:
        raise ContractError("unsupported fixture schema")
    normalized = _normalize_fixture(fixture)
    workspace = _id(normalized.get("workspace_hash"), "workspace_hash")
    agent_id = _id(normalized.get("agent_id"), "agent_id")
    as_of = _positive(normalized.get("as_of_unix_ms"), "as_of_unix_ms")
    _id(normalized.get("fixture_id"), "fixture_id")
    config = normalized.get("config")
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise ContractError("config fields do not match the v1 contract")
    for field in ("retrieval_mode", "reader", "judge"):
        _id(config[field], f"config.{field}")
    for field in ("top_k", "context_token_budget", "scan_budget", "seed"):
        _positive(config[field], f"config.{field}")

    raw_candidates = normalized.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ContractError("fixture candidates must be non-empty")
    candidates: dict[str, dict[str, Any]] = {}
    ref_groups: dict[str, str] = {}
    for index, candidate in enumerate(raw_candidates):
        if not isinstance(candidate, dict) or set(candidate) != CANDIDATE_FIELDS:
            raise ContractError(f"candidate {index} fields are malformed")
        candidate_id = _id(candidate["candidate_id"], f"candidate {index}.candidate_id")
        if candidate_id in candidates:
            raise ContractError("candidate IDs must be unique")
        for field in (
            "source_group",
            "source_ref",
            "lane",
            "workspace_hash",
            "agent_id",
        ):
            _id(candidate[field], f"candidate {index}.{field}")
        if candidate["lifecycle"] not in {"active", "superseded", "tombstoned"}:
            raise ContractError("candidate lifecycle is invalid")
        if not isinstance(candidate["available"], bool):
            raise ContractError("candidate available must be boolean")
        for field in ("valid_from_unix_ms", "token_count", "rank"):
            _positive(candidate[field], f"candidate {index}.{field}")
        end = candidate["valid_to_unix_ms"]
        if end is not None:
            _positive(end, f"candidate {index}.valid_to_unix_ms")
            if end <= candidate["valid_from_unix_ms"]:
                raise ContractError("candidate validity interval is inverted")
        prior_group = ref_groups.setdefault(
            candidate["source_ref"], candidate["source_group"]
        )
        if prior_group != candidate["source_group"]:
            raise ContractError("source_ref resolves ambiguously across source groups")
        candidates[candidate_id] = candidate

    raw_cases = normalized.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ContractError("fixture cases must be non-empty")
    case_ids: set[str] = set()
    for index, case in enumerate(raw_cases):
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            raise ContractError(f"case {index} fields are malformed")
        case_id = _id(case["case_id"], f"case {index}.case_id")
        if case_id in case_ids:
            raise ContractError("case IDs must be unique")
        case_ids.add(case_id)
        if case["workspace_hash"] != workspace or case["agent_id"] != agent_id:
            raise ContractError("case scope must match the frozen fixture scope")
        if case["as_of_unix_ms"] != as_of:
            raise ContractError("case as_of must match the frozen fixture anchor")
        if not isinstance(case["question"], str) or not case["question"].strip():
            raise ContractError("case question must be non-empty")
        candidate_ids = case["candidate_ids"]
        receipt_ids = case["receipt_candidate_ids"]
        if (
            not isinstance(candidate_ids, list)
            or not candidate_ids
            or len(candidate_ids) != len(set(candidate_ids))
        ):
            raise ContractError("case candidate_ids must be non-empty and unique")
        if any(candidate_id not in candidates for candidate_id in candidate_ids):
            raise ContractError("case references an unknown candidate")
        if (
            not isinstance(receipt_ids, list)
            or not receipt_ids
            or len(receipt_ids) != len(set(receipt_ids))
        ):
            raise ContractError("receipt_candidate_ids must be non-empty and unique")
        if any(candidate_id not in candidate_ids for candidate_id in receipt_ids):
            raise ContractError("receipt references a missing case candidate")
        evaluator = case["evaluator"]
        required = (
            evaluator.get("required_source_groups")
            if isinstance(evaluator, dict)
            else None
        )
        if (
            not isinstance(required, list)
            or not required
            or len(required) != len(set(required))
        ):
            raise ContractError(
                "evaluator required_source_groups must be non-empty and unique"
            )
        for group in required:
            _id(group, "evaluator.required_source_groups")
        receipt_groups: set[str] = set()
        for candidate_id in receipt_ids:
            candidate = candidates[candidate_id]
            reason = _candidate_eligibility(
                candidate, workspace=workspace, agent_id=agent_id, as_of=as_of
            )
            if reason is not None:
                raise ContractError(f"receipt reference is not eligible: {reason}")
            if candidate["source_group"] in receipt_groups:
                raise ContractError("receipt repeats a source group")
            receipt_groups.add(candidate["source_group"])
    return normalized


def _select(
    case: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    config: dict[str, Any],
    *,
    blocked_groups: set[str],
    arm_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        (candidates[candidate_id] for candidate_id in case["candidate_ids"]),
        key=lambda item: (item["rank"], item["candidate_id"]),
    )
    decisions: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    selected_groups: set[str] = set()
    delivered_tokens = 0
    for scan_index, candidate in enumerate(ordered, 1):
        if scan_index > config["scan_budget"]:
            decisions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "source_group": candidate["source_group"],
                    "source_ref": candidate["source_ref"],
                    "lane": candidate["lane"],
                    "disposition": "scan_budget_exhausted",
                }
            )
            continue
        if candidate["source_group"] in blocked_groups:
            disposition = (
                "blocked_receipt_source_group"
                if arm_id == "receipt-blocked"
                else "blocked_control_source_group"
            )
        else:
            disposition = _candidate_eligibility(
                candidate,
                workspace=case["workspace_hash"],
                agent_id=case["agent_id"],
                as_of=case["as_of_unix_ms"],
            )
            if disposition is None and candidate["source_group"] in selected_groups:
                disposition = "duplicate_source_group"
            if disposition is None and len(selected) >= config["top_k"]:
                disposition = "dropped_top_k"
            if (
                disposition is None
                and delivered_tokens + candidate["token_count"]
                > config["context_token_budget"]
            ):
                disposition = "dropped_context_budget"
            if disposition is None:
                disposition = "selected"
                selected.append(candidate)
                selected_groups.add(candidate["source_group"])
                delivered_tokens += candidate["token_count"]
        decisions.append(
            {
                "candidate_id": candidate["candidate_id"],
                "source_group": candidate["source_group"],
                "source_ref": candidate["source_ref"],
                "lane": candidate["lane"],
                "disposition": disposition,
            }
        )
    return selected, decisions


def _group_tokens(
    case: dict[str, Any], candidates: dict[str, dict[str, Any]]
) -> dict[str, int]:
    result: dict[str, int] = {}
    for candidate_id in case["candidate_ids"]:
        candidate = candidates[candidate_id]
        result.setdefault(candidate["source_group"], candidate["token_count"])
    return result


def _control_groups(
    case: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    config: dict[str, Any],
    receipt_groups: set[str],
    receipt_tokens: int,
) -> tuple[list[str], list[str]]:
    eligible_groups = sorted(
        {
            candidates[candidate_id]["source_group"]
            for candidate_id in case["candidate_ids"]
            if candidates[candidate_id]["source_group"] not in receipt_groups
            and _candidate_eligibility(
                candidates[candidate_id],
                workspace=case["workspace_hash"],
                agent_id=case["agent_id"],
                as_of=case["as_of_unix_ms"],
            )
            is None
        }
    )
    count = len(receipt_groups)
    if len(eligible_groups) < count:
        raise ContractError("insufficient eligible groups for matched controls")
    random_groups = sorted(
        eligible_groups,
        key=lambda group: sha256_text(f"{config['seed']}:{case['case_id']}:{group}"),
    )[:count]
    tokens = _group_tokens(case, candidates)
    matched_groups: list[str] | None = None
    for combo in itertools.combinations(eligible_groups, count):
        if sum(tokens[group] for group in combo) == receipt_tokens:
            matched_groups = list(combo)
            break
    if matched_groups is None:
        raise ContractError("no same-cardinality same-token control exists")
    return random_groups, matched_groups


def _seal_receipt(
    case: dict[str, Any],
    baseline_selected: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    config_sha256: str,
) -> dict[str, Any]:
    selected_ids = {candidate["candidate_id"] for candidate in baseline_selected}
    if any(
        candidate_id not in selected_ids
        for candidate_id in case["receipt_candidate_ids"]
    ):
        raise ContractError(
            "receipt reference is absent from the sealed baseline selection"
        )
    receipt_candidates = [
        candidates[candidate_id] for candidate_id in case["receipt_candidate_ids"]
    ]
    base = {
        "schema_version": "perseus-vault-evidence-intervention-receipt/v1",
        "case_id": case["case_id"],
        "workspace_hash": case["workspace_hash"],
        "agent_id": case["agent_id"],
        "as_of_unix_ms": case["as_of_unix_ms"],
        "baseline_selection_sha256": sha256_json(
            [candidate["candidate_id"] for candidate in baseline_selected]
        ),
        "receipt_candidate_ids": sorted(case["receipt_candidate_ids"]),
        "receipt_source_groups": sorted(
            candidate["source_group"] for candidate in receipt_candidates
        ),
        "receipt_source_refs": sorted(
            candidate["source_ref"] for candidate in receipt_candidates
        ),
        "config_sha256": config_sha256,
        "sealed_before_intervention": True,
    }
    return {**base, "receipt_sha256": sha256_json(base)}


def _intervention(
    arm_id: str,
    blocked_groups: list[str],
    *,
    group_tokens: dict[str, int],
    receipt_sha256: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "arm_id": arm_id,
        "blocked_source_groups": sorted(blocked_groups),
        "blocked_cardinality": len(blocked_groups),
        "blocked_tokens": sum(group_tokens[group] for group in blocked_groups),
        "scan_budget": config["scan_budget"],
        "context_token_budget": config["context_token_budget"],
        "receipt_sha256": receipt_sha256,
    }
    return {**base, "intervention_sha256": sha256_json(base)}


def _arm_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    required_total = sum(row["retrieval"]["required_count"] for row in rows)
    selected_required = sum(row["retrieval"]["selected_required_count"] for row in rows)
    return {
        "retrieval_sufficiency": {
            "denominator": required_total,
            "selected_required_count": selected_required,
            "required_evidence_rate": round(selected_required / required_total, 6)
            if required_total
            else None,
        },
        "context_assembly": {
            "denominator": len(rows),
            "selected_count": sum(row["context"]["selected_count"] for row in rows),
            "delivered_tokens": sum(row["context"]["delivered_tokens"] for row in rows),
            "assembly_error_count": sum(
                row["context"]["assembly_errors"] for row in rows
            ),
        },
        "answer_outcomes": {
            "denominator": len(rows),
            "supported": sum(row["answer_outcome"] == "supported" for row in rows),
            "insufficient_evidence": sum(
                row["answer_outcome"] == "insufficient_evidence" for row in rows
            ),
            "abstained": sum(row["answer_outcome"] == "abstained" for row in rows),
        },
        "execution": {
            "provider_calls": 0,
            "answerer_calls": 0,
            "judge_calls": 0,
            "case_denominator": len(rows),
        },
    }


def build_report(fixture: dict[str, Any]) -> dict[str, Any]:
    normalized = _validate_fixture(fixture)
    candidates = {
        candidate["candidate_id"]: candidate for candidate in normalized["candidates"]
    }
    config = normalized["config"]
    config_sha256 = sha256_json(config)

    # Baseline selection and receipt sealing are completed for every case before
    # any intervention set is selected. Gold remains untouched in evaluator input.
    case_state: dict[str, dict[str, Any]] = {}
    for case in normalized["cases"]:
        baseline_selected, baseline_decisions = _select(
            case, candidates, config, blocked_groups=set(), arm_id="baseline"
        )
        receipt = _seal_receipt(case, baseline_selected, candidates, config_sha256)
        case_state[case["case_id"]] = {
            "case": case,
            "baseline_selected": baseline_selected,
            "baseline_decisions": baseline_decisions,
            "receipt": receipt,
        }

    for state in case_state.values():
        case = state["case"]
        receipt = state["receipt"]
        receipt_groups = set(receipt["receipt_source_groups"])
        group_tokens = _group_tokens(case, candidates)
        receipt_tokens = sum(group_tokens[group] for group in receipt_groups)
        random_groups, matched_groups = _control_groups(
            case, candidates, config, receipt_groups, receipt_tokens
        )
        state["interventions"] = {
            "receipt-blocked": _intervention(
                "receipt-blocked",
                sorted(receipt_groups),
                group_tokens=group_tokens,
                receipt_sha256=receipt["receipt_sha256"],
                config=config,
            ),
            "random-control": _intervention(
                "random-control",
                random_groups,
                group_tokens=group_tokens,
                receipt_sha256=receipt["receipt_sha256"],
                config=config,
            ),
            "matched-size-control": _intervention(
                "matched-size-control",
                matched_groups,
                group_tokens=group_tokens,
                receipt_sha256=receipt["receipt_sha256"],
                config=config,
            ),
        }

    rows: list[dict[str, Any]] = []
    public_cases: list[dict[str, Any]] = []
    for case_id in sorted(case_state):
        state = case_state[case_id]
        case = state["case"]
        receipt = state["receipt"]
        receipt_groups = set(receipt["receipt_source_groups"])
        all_eligible_groups = {
            candidates[candidate_id]["source_group"]
            for candidate_id in case["candidate_ids"]
            if _candidate_eligibility(
                candidates[candidate_id],
                workspace=case["workspace_hash"],
                agent_id=case["agent_id"],
                as_of=case["as_of_unix_ms"],
            )
            is None
        }
        required_groups = set(case["evaluator"]["required_source_groups"])
        interventions = state["interventions"]
        public_cases.append(
            {
                "case_id": case_id,
                "query_sha256": sha256_text(case["question"]),
                "baseline_receipt": receipt,
                "interventions": interventions,
            }
        )
        for arm_id in ARM_IDS:
            if arm_id == "baseline":
                blocked_groups: set[str] = set()
                selected = state["baseline_selected"]
                decisions = state["baseline_decisions"]
            else:
                blocked_groups = set(interventions[arm_id]["blocked_source_groups"])
                selected, decisions = _select(
                    case,
                    candidates,
                    config,
                    blocked_groups=blocked_groups,
                    arm_id=arm_id,
                )
            selected_groups = [candidate["source_group"] for candidate in selected]
            selected_group_set = set(selected_groups)
            selected_required = required_groups & selected_group_set
            unavailable = required_groups - all_eligible_groups
            answer_outcome = (
                "supported"
                if required_groups <= selected_group_set
                else "abstained"
                if not selected_required
                else "insufficient_evidence"
            )
            alignment = (
                bool(selected_groups)
                if arm_id == "baseline"
                else bool(selected_group_set.isdisjoint(blocked_groups))
            )
            rows.append(
                {
                    "case_id": case_id,
                    "arm_id": arm_id,
                    "query_sha256": sha256_text(case["question"]),
                    "workspace_hash": case["workspace_hash"],
                    "agent_id": case["agent_id"],
                    "as_of_unix_ms": case["as_of_unix_ms"],
                    "blocked_source_groups": sorted(blocked_groups),
                    "selected_candidate_ids": [
                        candidate["candidate_id"] for candidate in selected
                    ],
                    "selected_source_groups": selected_groups,
                    "selected_token_counts": [
                        candidate["token_count"] for candidate in selected
                    ],
                    "candidate_decisions": decisions,
                    "retrieval": {
                        "required_count": len(required_groups),
                        "selected_required_count": len(selected_required),
                        "all_required_evidence": required_groups <= selected_group_set,
                    },
                    "context": {
                        "selected_count": len(selected),
                        "delivered_tokens": sum(
                            candidate["token_count"] for candidate in selected
                        ),
                        "token_budget": config["context_token_budget"],
                        "scan_budget": config["scan_budget"],
                        "assembly_errors": 0,
                    },
                    "evidence_accounting": {
                        "blocked_receipt_evidence_count": len(
                            receipt_groups & blocked_groups
                        ),
                        "blocked_gold_evidence_count": len(
                            required_groups & blocked_groups
                        ),
                        "selected_unreceipted_evidence_count": len(
                            (selected_group_set & required_groups) - receipt_groups
                        ),
                        "unavailable_evidence_count": len(unavailable),
                    },
                    "answer_outcome": answer_outcome,
                    "receipt_output_alignment": alignment,
                }
            )
    rows.sort(key=lambda row: (row["case_id"], row["arm_id"]))
    arms = []
    for arm_id in ARM_IDS:
        arm_rows = [row for row in rows if row["arm_id"] == arm_id]
        arms.append(
            {
                "arm_id": arm_id,
                "config": dict(config),
                "metrics": _arm_metrics(arm_rows),
            }
        )
    arms.sort(key=lambda arm: arm["arm_id"])
    question_set = [
        {"case_id": case["case_id"], "query_sha256": sha256_text(case["question"])}
        for case in normalized["cases"]
    ]
    receipt_set = [case["baseline_receipt"]["receipt_sha256"] for case in public_cases]
    intervention_set = [
        case["interventions"][arm_id]["intervention_sha256"]
        for case in public_cases
        for arm_id in INTERVENTION_IDS
    ]
    base: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "status": "complete",
        "benchmark": "receipt-conditioned-trace-faithfulness",
        "fixture_id": normalized["fixture_id"],
        "arms": arms,
        "cases": public_cases,
        "rows": rows,
        "commitments": {
            "fixture_sha256": sha256_json(normalized),
            "question_set_sha256": sha256_json(question_set),
            "retrieval_config_sha256": config_sha256,
            "baseline_receipt_set_sha256": sha256_json(receipt_set),
            "intervention_set_sha256": sha256_json(intervention_set),
            "output_projection_sha256": sha256_json(rows),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "evaluator_boundary": {
            "gold_available_only_after_receipt_seal": True,
            "gold_used_for_production_selection": False,
            "gold_used_for_intervention_selection": False,
        },
        "execution": {
            "mode": "provider-free-deterministic",
            "provider_calls": 0,
            "answerer_calls": 0,
            "judge_calls": 0,
            "network_calls": 0,
            "paid": False,
            "raw_external_payloads_captured": False,
        },
        "claims": {
            "label": "trace-faithfulness-evidence-necessity",
            "model_internal_causality": False,
            "third_party_benchmark_comparison": False,
            "existing_attribution_contract_controls_publication": True,
            "existing_counterfactual_gate_controls_publication": True,
        },
    }
    report = {**base, "report_sha256": sha256_json(base)}
    validate_report(report)
    return report


def validate_report(report: dict[str, Any]) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_SCHEMA:
        raise ContractError("unsupported report schema")
    if report.get("status") != "complete":
        raise ContractError("report is incomplete")
    digest = report.get("report_sha256")
    if not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
        raise ContractError("report digest is malformed")
    base = {key: value for key, value in report.items() if key != "report_sha256"}
    if sha256_json(base) != digest:
        raise ContractError("report digest mismatch")
    execution = report.get("execution", {})
    for field in ("provider_calls", "answerer_calls", "judge_calls", "network_calls"):
        if execution.get(field) != 0:
            raise ContractError("provider-free execution counters must be zero")
    if execution.get("paid") is not False:
        raise ContractError("provider-free execution cannot be paid")
    arms = report.get("arms")
    rows = report.get("rows")
    cases = report.get("cases")
    if (
        not isinstance(arms, list)
        or [arm.get("arm_id") for arm in arms] != sorted(ARM_IDS)
        or not isinstance(rows, list)
        or not rows
        or not isinstance(cases, list)
        or not cases
    ):
        raise ContractError("report paired structure is malformed")
    for arm in arms:
        arm_rows = [row for row in rows if row.get("arm_id") == arm["arm_id"]]
        if arm.get("metrics") != _arm_metrics(arm_rows):
            raise ContractError("arm metrics do not match rows")
    case_by_id = {case["case_id"]: case for case in cases}
    for case in cases:
        receipt = case.get("baseline_receipt")
        if (
            not isinstance(receipt, dict)
            or receipt.get("sealed_before_intervention") is not True
        ):
            raise ContractError("baseline receipt was not sealed")
        receipt_digest = receipt.get("receipt_sha256")
        receipt_base = {
            key: value for key, value in receipt.items() if key != "receipt_sha256"
        }
        if receipt_digest != sha256_json(receipt_base):
            raise ContractError("baseline receipt digest mismatch")
        interventions = case.get("interventions")
        if not isinstance(interventions, dict) or set(interventions) != set(
            INTERVENTION_IDS
        ):
            raise ContractError("intervention set is incomplete")
        for arm_id, intervention in interventions.items():
            intervention_base = {
                key: value
                for key, value in intervention.items()
                if key != "intervention_sha256"
            }
            if intervention.get("arm_id") != arm_id:
                raise ContractError("intervention arm mismatch")
            if intervention.get("receipt_sha256") != receipt_digest:
                raise ContractError("intervention is not bound to the baseline receipt")
            if intervention.get("intervention_sha256") != sha256_json(
                intervention_base
            ):
                raise ContractError("intervention digest mismatch")
    for row in rows:
        if row.get("case_id") not in case_by_id or row.get("arm_id") not in ARM_IDS:
            raise ContractError("row identity is invalid")
        selected_groups = row.get("selected_source_groups")
        selected_counts = row.get("selected_token_counts")
        context = row.get("context")
        if (
            not isinstance(selected_groups, list)
            or len(selected_groups) != len(set(selected_groups))
            or not isinstance(selected_counts, list)
            or not isinstance(context, dict)
        ):
            raise ContractError("row context projection is malformed")
        if context.get("selected_count") != len(selected_groups):
            raise ContractError("selected count mismatch")
        if context.get("delivered_tokens") != sum(selected_counts):
            raise ContractError("delivered token count mismatch")
        if context["delivered_tokens"] > context["token_budget"]:
            raise ContractError("context token budget exceeded")
        if row["arm_id"] != "baseline" and set(selected_groups) & set(
            row.get("blocked_source_groups", [])
        ):
            raise ContractError("blocked evidence re-entered the output")
        if row.get("receipt_output_alignment") is not True:
            raise ContractError("receipt/output alignment failed")
    commitments = report.get("commitments", {})
    for field in (
        "fixture_sha256",
        "question_set_sha256",
        "retrieval_config_sha256",
        "baseline_receipt_set_sha256",
        "intervention_set_sha256",
        "output_projection_sha256",
        "harness_sha256",
    ):
        if not isinstance(commitments.get(field), str) or not SHA_RE.fullmatch(
            commitments[field]
        ):
            raise ContractError(f"commitment {field} is malformed")
    if commitments["output_projection_sha256"] != sha256_json(rows):
        raise ContractError("output projection commitment mismatch")
    question_set = [
        {"case_id": case["case_id"], "query_sha256": case["query_sha256"]}
        for case in cases
    ]
    receipt_set = [case["baseline_receipt"]["receipt_sha256"] for case in cases]
    intervention_set = [
        case["interventions"][arm_id]["intervention_sha256"]
        for case in cases
        for arm_id in INTERVENTION_IDS
    ]
    if commitments["question_set_sha256"] != sha256_json(question_set):
        raise ContractError("question set commitment mismatch")
    if commitments["baseline_receipt_set_sha256"] != sha256_json(receipt_set):
        raise ContractError("baseline receipt set commitment mismatch")
    if commitments["intervention_set_sha256"] != sha256_json(intervention_set):
        raise ContractError("intervention set commitment mismatch")
    arm_configs = [arm.get("config") for arm in arms]
    if not arm_configs or any(config != arm_configs[0] for config in arm_configs[1:]):
        raise ContractError("paired arm configurations diverge")
    if commitments["retrieval_config_sha256"] != sha256_json(arm_configs[0]):
        raise ContractError("retrieval config commitment mismatch")
    if (
        commitments["harness_sha256"]
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise ContractError("harness commitment mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", default=str(Path(__file__).with_name("fixture.json"))
    )
    parser.add_argument("--out", default=str(Path(__file__).with_name("report.json")))
    args = parser.parse_args(argv)
    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    report = build_report(fixture)
    Path(args.out).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": report["status"], "report_sha256": report["report_sha256"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
