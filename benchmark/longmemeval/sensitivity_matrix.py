"""Provider-free reader/judge sensitivity matrix for LongMemEval.

This module is an additive protocol gate.  It does not run a reader, judge,
retrieval engine, or provider.  Inputs are already-sanitized synthetic or
controlled-custody outcomes; public output contains only bounded metadata,
digests, and metrics."""
from __future__ import annotations

import copy as _s1138_copy
import hashlib as _s1138_hashlib
import json as _s1138_json
import math as _s1138_math
import re as _s1138_re
from typing import Any as _s1138_Any
from pathlib import Path as _s1138_Path
from typing import Mapping as _s1138_Mapping

_s1138_SCHEMA_VERSION = "perseus-vault-longmemeval-sensitivity/v1"
_s1138_SCHEMA_ID = "https://perseus.observer/schemas/longmemeval-sensitivity-matrix-v1.json"
_s1138_ID_RE = _s1138_re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_s1138_SHA_RE = _s1138_re.compile(r"^[0-9a-f]{64}$")
_s1138_PROMPT_LANES = {"official-cot", "production-generic"}
_s1138_JUDGE_RELATIONS = {"correlated-same-model", "independent-model"}
_s1138_VERDICTS = {"correct", "incorrect", "answer_error", "judge_error"}
_s1138_LABELS = {"yes", "no", "unavailable"}
_s1138_ERRORS = {"answer_error", "judge_error"}
_s1138_FORBIDDEN_MARKERS = (
    "raw_prompt", "memory_body", "answer_text", "response_text",
    "judge_raw", "api_key", "credential", "password",
)
_s1138_COMMITMENT_FIELDS = (
    "dataset_sha256", "fixture_sha256", "config_sha256",
    "code_sha256", "run_sha256", "custody_sha256",
)
_s1138_OUTCOME_FIELDS = {
    "question_id_sha256", "question_type", "retrieval_hit",
    "retrieval_rank", "answer_verdict", "abstained",
    "primary_judge_label", "independent_judge_label",
    "answer_tokens_est", "retrieval_latency_ms",
    "answer_latency_ms", "judge_latency_ms", "calls", "usage",
    "answer_digest_sha256", "judge_digest_sha256", "exclusion_reason",
}
_s1138_CALL_FIELDS = {"retrieval", "answerer", "judge"}
_s1138_USAGE_FIELDS = {
    "answer_prompt_tokens", "answer_completion_tokens",
    "judge_prompt_tokens", "judge_completion_tokens",
    "provider_cost_microusd",
}


class _s1138_SensitivityValidationError(ValueError):
    """Raised when a matrix input or public report violates the contract."""


def _s1138_stable_json(value: _s1138_Any) -> str:
    try:
        return _s1138_json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _s1138_SensitivityValidationError("value is not canonical JSON") from exc


def _s1138_sha256_text(value: str) -> str:
    if not isinstance(value, str):
        raise _s1138_SensitivityValidationError("digest input must be text")
    return _s1138_hashlib.sha256(value.encode("utf-8")).hexdigest()


def _s1138_digest(value: _s1138_Any, field: str) -> str:
    if not isinstance(value, str) or not _s1138_SHA_RE.fullmatch(value):
        raise _s1138_SensitivityValidationError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _s1138_id(value: _s1138_Any, field: str) -> str:
    if not isinstance(value, str) or not _s1138_ID_RE.fullmatch(value):
        raise _s1138_SensitivityValidationError(f"{field} must be a bounded public identifier")
    lowered = value.lower()
    if any(marker in lowered for marker in ("password", "secret", "credential", "api_key", "authorization")):
        raise _s1138_SensitivityValidationError(f"{field} contains a forbidden private marker")
    return value


def _s1138_expect_keys(value: _s1138_Any, required: set[str], allowed: set[str], field: str) -> None:
    if not isinstance(value, _s1138_Mapping):
        raise _s1138_SensitivityValidationError(f"{field} must be an object")
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        raise _s1138_SensitivityValidationError(f"{field} is missing {missing[0]}")
    unknown = sorted(keys - allowed)
    if unknown:
        raise _s1138_SensitivityValidationError(f"{field} contains unknown field: {unknown[0]}")


def _s1138_nonnegative_int(value: _s1138_Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _s1138_SensitivityValidationError(f"{field} must be a non-negative integer")
    return value


def _s1138_positive_int(value: _s1138_Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _s1138_SensitivityValidationError(f"{field} must be a positive integer")
    return value


def _s1138_fraction(value: _s1138_Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _s1138_SensitivityValidationError(f"{field} must be numeric")
    result = float(value)
    if not _s1138_math.isfinite(result) or result < 0 or result > 1:
        raise _s1138_SensitivityValidationError(f"{field} must be finite in [0, 1]")
    return result


def _s1138_rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _s1138_mean(values: list[int]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _s1138_p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, _s1138_math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _s1138_validate_decoding(value: _s1138_Any, field: str) -> None:
    allowed = {"temperature", "top_p", "seed", "completion_cap"}
    _s1138_expect_keys(value, allowed, allowed, field)
    temperature = value["temperature"]
    top_p = value["top_p"]
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not _s1138_math.isfinite(float(temperature)) or not 0 <= float(temperature) <= 2:
        raise _s1138_SensitivityValidationError(f"{field}.temperature is invalid")
    if isinstance(top_p, bool) or not isinstance(top_p, (int, float)) or not _s1138_math.isfinite(float(top_p)) or not 0 < float(top_p) <= 1:
        raise _s1138_SensitivityValidationError(f"{field}.top_p is invalid")
    seed = value["seed"]
    if seed is not None:
        _s1138_nonnegative_int(seed, f"{field}.seed")
    _s1138_positive_int(value["completion_cap"], f"{field}.completion_cap")


def _s1138_validate_reader(value: _s1138_Any, field: str) -> None:
    allowed = {"provider", "model", "decoding"}
    _s1138_expect_keys(value, allowed, allowed, field)
    _s1138_id(value["provider"], f"{field}.provider")
    _s1138_id(value["model"], f"{field}.model")
    _s1138_validate_decoding(value["decoding"], f"{field}.decoding")


def _s1138_validate_judge(value: _s1138_Any, field: str) -> None:
    allowed = {
        "provider", "model", "decoding", "contract_id",
        "prompt_digest_sha256", "threshold", "relation",
        "independence_claim_eligible",
    }
    _s1138_expect_keys(value, allowed, allowed, field)
    _s1138_id(value["provider"], f"{field}.provider")
    _s1138_id(value["model"], f"{field}.model")
    _s1138_validate_decoding(value["decoding"], f"{field}.decoding")
    _s1138_id(value["contract_id"], f"{field}.contract_id")
    _s1138_digest(value["prompt_digest_sha256"], f"{field}.prompt_digest_sha256")
    threshold = value["threshold"]
    _s1138_expect_keys(threshold, {"kind", "value", "labels"}, {"kind", "value", "labels"}, f"{field}.threshold")
    _s1138_id(threshold["kind"], f"{field}.threshold.kind")
    _s1138_fraction(threshold["value"], f"{field}.threshold.value")
    labels = threshold["labels"]
    if not isinstance(labels, list) or len(labels) != 2 or len(set(labels)) != 2 or set(labels) != {"yes", "no"}:
        raise _s1138_SensitivityValidationError(f"{field}.threshold.labels must be yes/no")
    if value["relation"] not in _s1138_JUDGE_RELATIONS:
        raise _s1138_SensitivityValidationError(f"{field}.relation is invalid")
    if not isinstance(value["independence_claim_eligible"], bool):
        raise _s1138_SensitivityValidationError(f"{field}.independence_claim_eligible must be boolean")


def _s1138_validate_prompt(value: _s1138_Any, field: str) -> None:
    allowed = {"lane", "version", "digest_sha256"}
    _s1138_expect_keys(value, allowed, allowed, field)
    if value["lane"] not in _s1138_PROMPT_LANES:
        raise _s1138_SensitivityValidationError(f"{field}.lane is invalid")
    _s1138_id(value["version"], f"{field}.version")
    _s1138_digest(value["digest_sha256"], f"{field}.digest_sha256")


def _s1138_validate_retrieval(value: _s1138_Any, field: str) -> None:
    allowed = {
        "arm", "mode", "requested_depth", "effective_depth",
        "context_token_budget", "assembly_policy",
    }
    _s1138_expect_keys(value, allowed, allowed, field)
    _s1138_id(value["arm"], f"{field}.arm")
    _s1138_id(value["mode"], f"{field}.mode")
    requested = _s1138_positive_int(value["requested_depth"], f"{field}.requested_depth")
    effective = _s1138_positive_int(value["effective_depth"], f"{field}.effective_depth")
    if effective > requested:
        raise _s1138_SensitivityValidationError(f"{field}.effective_depth exceeds requested_depth")
    _s1138_positive_int(value["context_token_budget"], f"{field}.context_token_budget")
    _s1138_id(value["assembly_policy"], f"{field}.assembly_policy")


def _s1138_validate_retry(value: _s1138_Any, field: str) -> None:
    allowed = {"max_retries", "on_error"}
    _s1138_expect_keys(value, allowed, allowed, field)
    _s1138_nonnegative_int(value["max_retries"], f"{field}.max_retries")
    _s1138_id(value["on_error"], f"{field}.on_error")


def _s1138_validate_denominator(value: _s1138_Any, field: str) -> None:
    allowed = {"name", "excluded_errors"}
    _s1138_expect_keys(value, allowed, allowed, field)
    _s1138_id(value["name"], f"{field}.name")
    errors = value["excluded_errors"]
    if not isinstance(errors, list) or len(set(errors)) != len(errors) or set(errors) != _s1138_ERRORS:
        raise _s1138_SensitivityValidationError(f"{field}.excluded_errors must name both error states")


def _s1138_validate_commitments(value: _s1138_Any, field: str) -> None:
    allowed = set(_s1138_COMMITMENT_FIELDS)
    _s1138_expect_keys(value, allowed, allowed, field)
    for name in _s1138_COMMITMENT_FIELDS:
        _s1138_digest(value[name], f"{field}.{name}")


def _s1138_validate_calls(value: _s1138_Any, field: str, provider_free: bool) -> None:
    _s1138_expect_keys(value, _s1138_CALL_FIELDS, _s1138_CALL_FIELDS, field)
    for name in sorted(_s1138_CALL_FIELDS):
        count = _s1138_nonnegative_int(value[name], f"{field}.{name}")
        if provider_free and count != 0:
            raise _s1138_SensitivityValidationError(f"{field}.{name} must be zero in provider-free mode")


def _s1138_validate_usage(value: _s1138_Any, field: str, provider_free: bool) -> None:
    _s1138_expect_keys(value, _s1138_USAGE_FIELDS, _s1138_USAGE_FIELDS, field)
    for name in sorted(_s1138_USAGE_FIELDS):
        count = _s1138_nonnegative_int(value[name], f"{field}.{name}")
        if provider_free and count != 0:
            raise _s1138_SensitivityValidationError(f"{field}.{name} must be zero in provider-free mode")


def _s1138_validate_outcome(value: _s1138_Any, field: str, effective_depth: int, provider_free: bool) -> dict[str, _s1138_Any]:
    _s1138_expect_keys(value, _s1138_OUTCOME_FIELDS, _s1138_OUTCOME_FIELDS, field)
    _s1138_digest(value["question_id_sha256"], f"{field}.question_id_sha256")
    _s1138_id(value["question_type"], f"{field}.question_type")
    if not isinstance(value["retrieval_hit"], bool):
        raise _s1138_SensitivityValidationError(f"{field}.retrieval_hit must be boolean")
    rank = value["retrieval_rank"]
    if rank is not None:
        _s1138_positive_int(rank, f"{field}.retrieval_rank")
    if value["retrieval_hit"] and (rank is None or rank > effective_depth):
        raise _s1138_SensitivityValidationError(f"{field}.retrieval_hit has no valid in-depth rank")
    if value["answer_verdict"] not in _s1138_VERDICTS:
        raise _s1138_SensitivityValidationError(f"{field}.answer_verdict is invalid")
    if not isinstance(value["abstained"], bool):
        raise _s1138_SensitivityValidationError(f"{field}.abstained must be boolean")
    if value["primary_judge_label"] not in _s1138_LABELS:
        raise _s1138_SensitivityValidationError(f"{field}.primary_judge_label is invalid")
    independent = value["independent_judge_label"]
    if independent is not None and independent not in _s1138_LABELS:
        raise _s1138_SensitivityValidationError(f"{field}.independent_judge_label is invalid")
    expected_label = {"correct": "yes", "incorrect": "no", "answer_error": "unavailable", "judge_error": "unavailable"}[value["answer_verdict"]]
    if value["primary_judge_label"] != expected_label:
        raise _s1138_SensitivityValidationError(f"{field}.primary_judge_label conflicts with answer_verdict")
    reason = value["exclusion_reason"]
    if value["answer_verdict"] in _s1138_ERRORS:
        if reason != value["answer_verdict"]:
            raise _s1138_SensitivityValidationError(f"{field}.exclusion_reason must match excluded verdict")
    elif reason is not None:
        raise _s1138_SensitivityValidationError(f"{field}.exclusion_reason is only valid for errors")
    _s1138_nonnegative_int(value["answer_tokens_est"], f"{field}.answer_tokens_est")
    for name in ("retrieval_latency_ms", "answer_latency_ms", "judge_latency_ms"):
        _s1138_nonnegative_int(value[name], f"{field}.{name}")
    _s1138_validate_calls(value["calls"], f"{field}.calls", provider_free)
    _s1138_validate_usage(value["usage"], f"{field}.usage", provider_free)
    _s1138_digest(value["answer_digest_sha256"], f"{field}.answer_digest_sha256")
    _s1138_digest(value["judge_digest_sha256"], f"{field}.judge_digest_sha256")
    return _s1138_copy.deepcopy(dict(value))


def _s1138_validate_dataset(value: _s1138_Any) -> None:
    allowed = {"name", "split", "digest_sha256", "question_count", "question_type_distribution"}
    _s1138_expect_keys(value, allowed, allowed, "dataset")
    _s1138_id(value["name"], "dataset.name")
    _s1138_id(value["split"], "dataset.split")
    _s1138_digest(value["digest_sha256"], "dataset.digest_sha256")
    count = _s1138_positive_int(value["question_count"], "dataset.question_count")
    distribution = value["question_type_distribution"]
    if not isinstance(distribution, _s1138_Mapping) or not distribution:
        raise _s1138_SensitivityValidationError("dataset.question_type_distribution must be non-empty")
    total = 0
    for question_type, number in distribution.items():
        _s1138_id(question_type, "dataset.question_type_distribution key")
        total += _s1138_nonnegative_int(number, f"dataset.question_type_distribution.{question_type}")
    if total != count:
        raise _s1138_SensitivityValidationError("dataset distribution does not sum to question_count")


def _s1138_validate_execution(value: _s1138_Any) -> bool:
    allowed = {"mode", "network_calls", "provider_calls", "paid", "raw_provider_payloads_captured"}
    _s1138_expect_keys(value, allowed, allowed, "execution")
    _s1138_id(value["mode"], "execution.mode")
    provider_free = value["mode"] == "provider-free-synthetic"
    _s1138_nonnegative_int(value["network_calls"], "execution.network_calls")
    _s1138_nonnegative_int(value["provider_calls"], "execution.provider_calls")
    if not isinstance(value["paid"], bool) or not isinstance(value["raw_provider_payloads_captured"], bool):
        raise _s1138_SensitivityValidationError("execution booleans are invalid")
    if provider_free and (value["network_calls"] != 0 or value["provider_calls"] != 0 or value["paid"] or value["raw_provider_payloads_captured"]):
        raise _s1138_SensitivityValidationError("provider-free execution must have zero calls and no raw payloads")
    return provider_free


def _s1138_validate_root_input(value: _s1138_Any) -> bool:
    allowed = {
        "schema_version", "matrix_id", "dataset", "baseline_cell_id",
        "required_cell_ids", "commitments", "execution", "validation", "cells",
    }
    _s1138_expect_keys(value, allowed, allowed, "matrix")
    if value["schema_version"] != _s1138_SCHEMA_VERSION:
        raise _s1138_SensitivityValidationError("unsupported sensitivity matrix schema")
    _s1138_id(value["matrix_id"], "matrix_id")
    _s1138_validate_dataset(value["dataset"])
    required = value["required_cell_ids"]
    if not isinstance(required, list) or len(required) < 2 or len(set(required)) != len(required):
        raise _s1138_SensitivityValidationError("required_cell_ids must contain unique cells")
    for cell_id in required:
        _s1138_id(cell_id, "required_cell_ids item")
    _s1138_id(value["baseline_cell_id"], "baseline_cell_id")
    if value["baseline_cell_id"] not in required:
        raise _s1138_SensitivityValidationError("baseline_cell_id is not required")
    _s1138_validate_commitments(value["commitments"], "commitments")
    provider_free = _s1138_validate_execution(value["execution"])
    validation = value["validation"]
    _s1138_expect_keys(validation, {"human_audit_sample_n", "second_judge_sample_n"}, {"human_audit_sample_n", "second_judge_sample_n"}, "validation")
    _s1138_nonnegative_int(validation["human_audit_sample_n"], "validation.human_audit_sample_n")
    _s1138_nonnegative_int(validation["second_judge_sample_n"], "validation.second_judge_sample_n")
    cells = value["cells"]
    if not isinstance(cells, list) or len(cells) != len(required):
        raise _s1138_SensitivityValidationError("cells must contain every required cell exactly once")
    ids = [cell.get("cell_id") for cell in cells if isinstance(cell, _s1138_Mapping)]
    if set(ids) != set(required) or len(ids) != len(set(ids)):
        raise _s1138_SensitivityValidationError("cells do not match required_cell_ids")
    return provider_free


def _s1138_validate_cell_input(value: _s1138_Any, provider_free: bool, field: str) -> tuple[dict[str, _s1138_Any], list[dict[str, _s1138_Any]]]:
    allowed = {
        "cell_id", "source", "reader", "judge", "prompt", "retrieval",
        "retry_policy", "denominator_policy", "artifact_commitments", "outcomes",
    }
    _s1138_expect_keys(value, allowed, allowed, field)
    _s1138_id(value["cell_id"], f"{field}.cell_id")
    _s1138_id(value["source"], f"{field}.source")
    if value["source"] == "baseline-post-processed":
        raise _s1138_SensitivityValidationError(f"{field}.source cannot post-process a baseline")
    _s1138_validate_reader(value["reader"], f"{field}.reader")
    _s1138_validate_judge(value["judge"], f"{field}.judge")
    same_model = (value["reader"]["provider"], value["reader"]["model"]) == (value["judge"]["provider"], value["judge"]["model"])
    expected_relation = "correlated-same-model" if same_model else "independent-model"
    if value["judge"]["relation"] != expected_relation:
        raise _s1138_SensitivityValidationError(f"{field}.judge.relation does not match reader/judge identity")
    if same_model and value["judge"]["independence_claim_eligible"]:
        raise _s1138_SensitivityValidationError(f"{field}.judge same-model result cannot claim independence")
    _s1138_validate_prompt(value["prompt"], f"{field}.prompt")
    _s1138_validate_retrieval(value["retrieval"], f"{field}.retrieval")
    _s1138_validate_retry(value["retry_policy"], f"{field}.retry_policy")
    _s1138_validate_denominator(value["denominator_policy"], f"{field}.denominator_policy")
    _s1138_validate_commitments(value["artifact_commitments"], f"{field}.artifact_commitments")
    outcomes = value["outcomes"]
    if not isinstance(outcomes, list) or not outcomes:
        raise _s1138_SensitivityValidationError(f"{field}.outcomes must be non-empty")
    effective_depth = value["retrieval"]["effective_depth"]
    normalized = [_s1138_validate_outcome(item, f"{field}.outcomes[{index}]", effective_depth, provider_free) for index, item in enumerate(outcomes)]
    if same_model and any(item["independent_judge_label"] is not None for item in normalized):
        raise _s1138_SensitivityValidationError(f"{field}.outcomes cannot claim an independent judge for a same-model cell")
    if len({item["question_id_sha256"] for item in normalized}) != len(normalized):
        raise _s1138_SensitivityValidationError(f"{field}.outcomes question IDs must be unique")
    return _s1138_copy.deepcopy(dict(value)), normalized


def _s1138_cell_config(cell: _s1138_Mapping[str, _s1138_Any]) -> dict[str, _s1138_Any]:
    return _s1138_copy.deepcopy({
        "cell_id": cell["cell_id"],
        "source": cell["source"],
        "reader": cell["reader"],
        "judge": cell["judge"],
        "prompt": cell["prompt"],
        "retrieval": cell["retrieval"],
        "retry_policy": cell["retry_policy"],
        "denominator_policy": cell["denominator_policy"],
        "artifact_commitments": cell["artifact_commitments"],
    })


def _s1138_metrics(outcomes: list[dict[str, _s1138_Any]], judge: _s1138_Mapping[str, _s1138_Any]) -> dict[str, _s1138_Any]:
    total = len(outcomes)
    hits = sum(int(item["retrieval_hit"]) for item in outcomes)
    ranks = [item["retrieval_rank"] for item in outcomes if item["retrieval_rank"] is not None]
    graded = [item for item in outcomes if item["answer_verdict"] in {"correct", "incorrect"}]
    correct = sum(item["answer_verdict"] == "correct" for item in graded)
    answer_errors = sum(item["answer_verdict"] == "answer_error" for item in outcomes)
    judge_errors = sum(item["answer_verdict"] == "judge_error" for item in outcomes)
    abstained = [item for item in outcomes if item["abstained"]]
    abstained_graded = [item for item in abstained if item["answer_verdict"] in {"correct", "incorrect"}]
    abstained_correct = sum(item["answer_verdict"] == "correct" for item in abstained_graded)
    independent = [item for item in outcomes if item["independent_judge_label"] in {"yes", "no"} and item["primary_judge_label"] in {"yes", "no"}]
    agreements = sum(item["primary_judge_label"] == item["independent_judge_label"] for item in independent)
    retrieval_latency = [item["retrieval_latency_ms"] for item in outcomes]
    answer_latency = [item["answer_latency_ms"] for item in outcomes]
    judge_latency = [item["judge_latency_ms"] for item in outcomes]
    calls = {name: sum(item["calls"][name] for item in outcomes) for name in sorted(_s1138_CALL_FIELDS)}
    usage = {name: sum(item["usage"][name] for item in outcomes) for name in sorted(_s1138_USAGE_FIELDS)}
    usage["total_tokens"] = sum(usage[name] for name in sorted(_s1138_USAGE_FIELDS) if name.endswith("tokens"))
    return {
        "retrieval": {
            "eligible_n": total, "hit_n": hits, "coverage": _s1138_rate(hits, total),
            "mean_rank": _s1138_mean(ranks),
        },
        "qa": {
            "attempted_n": total, "graded_n": len(graded), "correct_n": correct,
            "accuracy": _s1138_rate(correct, len(graded)),
            "answer_errors": answer_errors, "judge_errors": judge_errors,
            "excluded_n": answer_errors + judge_errors,
        },
        "abstention": {
            "attempted_n": total, "abstained_n": len(abstained),
            "abstention_rate": _s1138_rate(len(abstained), total),
            "graded_n": len(abstained_graded), "correct_n": abstained_correct,
            "accuracy": _s1138_rate(abstained_correct, len(abstained_graded)),
        },
        "negative_behavior": {
            "abstained_n": len(abstained),
            "incorrect_abstention_n": sum(item["answer_verdict"] == "incorrect" for item in abstained),
            "correct_abstention_n": abstained_correct,
            "incorrect_nonabstention_n": sum(item["answer_verdict"] == "incorrect" and not item["abstained"] for item in outcomes),
        },
        "judge": {
            "relation": judge["relation"],
            "same_model_correlated": judge["relation"] == "correlated-same-model",
            "independent_label_n": len(independent),
            "agreement_n": agreements,
            "discordance_n": len(independent) - agreements,
            "agreement_rate": _s1138_rate(agreements, len(independent)),
            "independence_claim_eligible": judge["independence_claim_eligible"],
        },
        "telemetry": {
            "latency_ms": {
                "retrieval_mean": _s1138_mean(retrieval_latency),
                "answer_mean": _s1138_mean(answer_latency),
                "judge_mean": _s1138_mean(judge_latency),
                "total_mean": _s1138_mean([a + b + c for a, b, c in zip(retrieval_latency, answer_latency, judge_latency)]),
                "retrieval_p95": _s1138_p95(retrieval_latency),
                "answer_p95": _s1138_p95(answer_latency),
                "judge_p95": _s1138_p95(judge_latency),
            },
            "calls": {**calls, "total": sum(calls.values())},
            "provider_usage": usage,
        },
    }


def _s1138_category_metrics(outcomes: list[dict[str, _s1138_Any]]) -> dict[str, dict[str, _s1138_Any]]:
    groups: dict[str, list[dict[str, _s1138_Any]]] = {}
    for item in outcomes:
        groups.setdefault(item["question_type"], []).append(item)
    result: dict[str, dict[str, _s1138_Any]] = {}
    for question_type in sorted(groups):
        group = groups[question_type]
        graded = [item for item in group if item["answer_verdict"] in {"correct", "incorrect"}]
        hits = sum(int(item["retrieval_hit"]) for item in group)
        correct = sum(item["answer_verdict"] == "correct" for item in graded)
        result[question_type] = {
            "n": len(group), "retrieval_hit_n": hits,
            "retrieval_coverage": _s1138_rate(hits, len(group)),
            "qa_graded_n": len(graded), "qa_correct_n": correct,
            "qa_accuracy": _s1138_rate(correct, len(graded)),
            "abstention_n": sum(item["abstained"] for item in group),
            "abstention_rate": _s1138_rate(sum(item["abstained"] for item in group), len(group)),
        }
    return result


def _s1138_pair_rows(cells: list[dict[str, _s1138_Any]]) -> list[dict[str, _s1138_Any]]:
    by_cell = {cell["cell_id"]: {item["question_id_sha256"]: item for item in cell["outcomes"]} for cell in cells}
    question_ids = set.intersection(*(set(rows) for rows in by_cell.values()))
    if any(set(rows) != question_ids for rows in by_cell.values()):
        raise _s1138_SensitivityValidationError("cells must contain the same complete question set")
    rows: list[dict[str, _s1138_Any]] = []
    for question_id in sorted(question_ids):
        items = [by_cell[cell_id][question_id] for cell_id in sorted(by_cell)]
        types = {item["question_type"] for item in items}
        if len(types) != 1:
            raise _s1138_SensitivityValidationError("paired cells disagree on question type")
        rows.append({
            "question_id_sha256": question_id,
            "question_type": items[0]["question_type"],
            "cells": [
                {
                    "cell_id": cell_id,
                    "retrieval_hit": by_cell[cell_id][question_id]["retrieval_hit"],
                    "retrieval_rank": by_cell[cell_id][question_id]["retrieval_rank"],
                    "answer_verdict": by_cell[cell_id][question_id]["answer_verdict"],
                    "abstained": by_cell[cell_id][question_id]["abstained"],
                    "excluded": by_cell[cell_id][question_id]["answer_verdict"] in _s1138_ERRORS,
                    "answer_tokens_est": by_cell[cell_id][question_id]["answer_tokens_est"],
                    "independent_judge_label": by_cell[cell_id][question_id]["independent_judge_label"],
                }
                for cell_id in sorted(by_cell)
            ],
        })
    return rows


def _s1138_delta(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None:
        return None
    return round(value - baseline, 4)


def _s1138_category_deltas(cells: list[dict[str, _s1138_Any]], baseline_cell_id: str) -> list[dict[str, _s1138_Any]]:
    by_id = {cell["cell_id"]: cell for cell in cells}
    question_types = sorted({question_type for cell in cells for question_type in cell["category_metrics"]})
    result = []
    for cell in sorted(cells, key=lambda item: item["cell_id"]):
        for question_type in question_types:
            current = cell["category_metrics"].get(question_type)
            baseline = by_id[baseline_cell_id]["category_metrics"].get(question_type)
            result.append({
                "cell_id": cell["cell_id"],
                "baseline_cell_id": baseline_cell_id,
                "question_type": question_type,
                "paired_n": current["n"] if current else 0,
                "baseline_paired_n": baseline["n"] if baseline else 0,
                "retrieval_coverage": current["retrieval_coverage"] if current else None,
                "baseline_retrieval_coverage": baseline["retrieval_coverage"] if baseline else None,
                "retrieval_coverage_delta": _s1138_delta(current["retrieval_coverage"] if current else None, baseline["retrieval_coverage"] if baseline else None),
                "qa_accuracy": current["qa_accuracy"] if current else None,
                "baseline_qa_accuracy": baseline["qa_accuracy"] if baseline else None,
                "qa_accuracy_delta": _s1138_delta(current["qa_accuracy"] if current else None, baseline["qa_accuracy"] if baseline else None),
                "abstention_rate": current["abstention_rate"] if current else None,
                "baseline_abstention_rate": baseline["abstention_rate"] if baseline else None,
                "abstention_rate_delta": _s1138_delta(current["abstention_rate"] if current else None, baseline["abstention_rate"] if baseline else None),
            })
    return result


def _s1138_sensitivity_table(cells: list[dict[str, _s1138_Any]], baseline_cell_id: str) -> list[dict[str, _s1138_Any]]:
    by_id = {cell["cell_id"]: cell for cell in cells}
    baseline_metrics = by_id[baseline_cell_id]["metrics"]
    result = []
    for cell in sorted(cells, key=lambda item: item["cell_id"]):
        metrics = cell["metrics"]
        result.append({
            "cell_id": cell["cell_id"],
            "reader_provider": cell["reader"]["provider"],
            "reader_model": cell["reader"]["model"],
            "judge_provider": cell["judge"]["provider"],
            "judge_model": cell["judge"]["model"],
            "judge_relation": cell["judge"]["relation"],
            "prompt_lane": cell["prompt"]["lane"],
            "retrieval_arm": cell["retrieval"]["arm"],
            "retrieval_mode": cell["retrieval"]["mode"],
            "requested_depth": cell["retrieval"]["requested_depth"],
            "effective_depth": cell["retrieval"]["effective_depth"],
            "context_token_budget": cell["retrieval"]["context_token_budget"],
            "retrieval_coverage": metrics["retrieval"]["coverage"],
            "qa_accuracy": metrics["qa"]["accuracy"],
            "abstention_rate": metrics["abstention"]["abstention_rate"],
            "retrieval_coverage_delta": _s1138_delta(metrics["retrieval"]["coverage"], baseline_metrics["retrieval"]["coverage"]),
            "qa_accuracy_delta": _s1138_delta(metrics["qa"]["accuracy"], baseline_metrics["qa"]["accuracy"]),
            "abstention_rate_delta": _s1138_delta(metrics["abstention"]["abstention_rate"], baseline_metrics["abstention"]["abstention_rate"]),
            "judge_discordance_n": metrics["judge"]["discordance_n"],
            "independence_claim_eligible": cell["judge"]["independence_claim_eligible"],
        })
    return result


def _s1138_build_report(value: _s1138_Mapping[str, _s1138_Any]) -> dict[str, _s1138_Any]:
    source = _s1138_copy.deepcopy(dict(value))
    provider_free = _s1138_validate_root_input(source)
    normalized_cells = []
    config_digests = set()
    question_types: dict[str, int] | None = None
    for cell in source["cells"]:
        cell_label = cell.get("cell_id", "?") if isinstance(cell, _s1138_Mapping) else "?"
        normalized, outcomes = _s1138_validate_cell_input(cell, provider_free, f"cells[{cell_label}]")
        normalized["outcomes"] = outcomes
        config = _s1138_cell_config(normalized)
        config_digest = _s1138_sha256_text(_s1138_stable_json(config))
        if config_digest in config_digests:
            raise _s1138_SensitivityValidationError("cells must have distinct configuration commitments")
        config_digests.add(config_digest)
        current_types = {}
        for outcome in outcomes:
            current_types[outcome["question_type"]] = current_types.get(outcome["question_type"], 0) + 1
        if question_types is None:
            question_types = current_types
        elif question_types != current_types:
            raise _s1138_SensitivityValidationError("cells must cover the same category distribution")
        outcomes = sorted(outcomes, key=lambda item: item["question_id_sha256"])
        public_cell = {**config, "outcomes": outcomes}
        public_cell["cell_config_sha256"] = config_digest
        public_cell["outcomes_sha256"] = _s1138_sha256_text(_s1138_stable_json(outcomes))
        public_cell["metrics"] = _s1138_metrics(outcomes, normalized["judge"])
        public_cell["category_metrics"] = _s1138_category_metrics(outcomes)
        normalized_cells.append(public_cell)
    normalized_cells.sort(key=lambda item: item["cell_id"])
    expected_distribution = dict(source["dataset"]["question_type_distribution"])
    if question_types != expected_distribution:
        raise _s1138_SensitivityValidationError("outcome categories do not match dataset distribution")
    baseline = source["baseline_cell_id"]
    correlated = sorted(cell["cell_id"] for cell in normalized_cells if cell["judge"]["relation"] == "correlated-same-model")
    validation_input = source["validation"]
    audit_allowed = bool(validation_input["human_audit_sample_n"] or validation_input["second_judge_sample_n"]) and not correlated
    validation = {
        "human_audit_sample_n": validation_input["human_audit_sample_n"],
        "second_judge_sample_n": validation_input["second_judge_sample_n"],
        "same_model_audit": {
            "correlated_cell_ids": correlated,
            "independent_validation_claim_allowed": False if correlated else audit_allowed,
            "finding": "same-model answerer/judge is correlated; it is not independent validation" if correlated else "no same-model correlated cell",
        },
        "smaller_reader_claim_allowed": audit_allowed,
    }
    report_base = {
        "schema_version": _s1138_SCHEMA_VERSION,
        "matrix_id": source["matrix_id"],
        "status": "complete",
        "dataset": source["dataset"],
        "baseline_cell_id": baseline,
        "required_cell_ids": sorted(source["required_cell_ids"]),
        "commitments": source["commitments"],
        "execution": source["execution"],
        "validation": validation,
        "cells": normalized_cells,
        "paired_rows": _s1138_pair_rows(normalized_cells),
        "category_deltas": _s1138_category_deltas(normalized_cells, baseline),
        "sensitivity_table": _s1138_sensitivity_table(normalized_cells, baseline),
    }
    report = {**report_base, "report_sha256": _s1138_sha256_text(_s1138_stable_json(report_base))}
    _s1138_validate_report(report)
    return report


def _s1138_validate_report(value: _s1138_Any) -> None:
    allowed = {
        "schema_version", "matrix_id", "status", "dataset",
        "baseline_cell_id", "required_cell_ids", "commitments",
        "execution", "validation", "cells", "paired_rows",
        "category_deltas", "sensitivity_table", "report_sha256",
    }
    _s1138_expect_keys(value, allowed, allowed, "report")
    if value["schema_version"] != _s1138_SCHEMA_VERSION or value["status"] != "complete":
        raise _s1138_SensitivityValidationError("report schema or status is invalid")
    _s1138_id(value["matrix_id"], "report.matrix_id")
    _s1138_validate_dataset(value["dataset"])
    _s1138_validate_commitments(value["commitments"], "report.commitments")
    provider_free = _s1138_validate_execution(value["execution"])
    required = value["required_cell_ids"]
    if not isinstance(required, list) or len(required) < 2 or len(set(required)) != len(required):
        raise _s1138_SensitivityValidationError("report.required_cell_ids is invalid")
    _s1138_id(value["baseline_cell_id"], "report.baseline_cell_id")
    if value["baseline_cell_id"] not in required:
        raise _s1138_SensitivityValidationError("report baseline is not required")
    cells = value["cells"]
    if not isinstance(cells, list) or {cell.get("cell_id") for cell in cells if isinstance(cell, _s1138_Mapping)} != set(required):
        raise _s1138_SensitivityValidationError("report cells do not match required cells")
    config_digests = set()
    question_sets = []
    for index, cell in enumerate(cells):
        field = f"report.cells[{index}]"
        allowed_cell = {
            "cell_id", "source", "reader", "judge", "prompt", "retrieval",
            "retry_policy", "denominator_policy", "artifact_commitments",
            "outcomes", "cell_config_sha256", "outcomes_sha256",
            "metrics", "category_metrics",
        }
        _s1138_expect_keys(cell, allowed_cell, allowed_cell, field)
        config, outcomes = _s1138_validate_cell_input({key: cell[key] for key in allowed_cell if key not in {"cell_config_sha256", "outcomes_sha256", "metrics", "category_metrics"}}, provider_free, field)
        del config
        _s1138_digest(cell["cell_config_sha256"], f"{field}.cell_config_sha256")
        _s1138_digest(cell["outcomes_sha256"], f"{field}.outcomes_sha256")
        expected_config = _s1138_cell_config(cell)
        if cell["cell_config_sha256"] != _s1138_sha256_text(_s1138_stable_json(expected_config)):
            raise _s1138_SensitivityValidationError(f"{field} configuration digest mismatch")
        ordered = sorted(outcomes, key=lambda item: item["question_id_sha256"])
        if ordered != cell["outcomes"]:
            raise _s1138_SensitivityValidationError(f"{field}.outcomes are not canonicalized")
        if cell["outcomes_sha256"] != _s1138_sha256_text(_s1138_stable_json(ordered)):
            raise _s1138_SensitivityValidationError(f"{field} outcome digest mismatch")
        if not isinstance(cell["metrics"], _s1138_Mapping) or not isinstance(cell["category_metrics"], _s1138_Mapping):
            raise _s1138_SensitivityValidationError(f"{field} metrics are malformed")
        if cell["metrics"] != _s1138_metrics(ordered, cell["judge"]):
            raise _s1138_SensitivityValidationError(f"{field} derived metrics mismatch")
        if cell["category_metrics"] != _s1138_category_metrics(ordered):
            raise _s1138_SensitivityValidationError(f"{field} category metrics mismatch")
        config_digests.add(cell["cell_config_sha256"])
        question_sets.append({item["question_id_sha256"] for item in outcomes})
    if len(config_digests) != len(cells) or any(question_set != question_sets[0] for question_set in question_sets[1:]):
        raise _s1138_SensitivityValidationError("report cells are not distinct complete paired cells")
    validation = value["validation"]
    validation_allowed = {"human_audit_sample_n", "second_judge_sample_n", "same_model_audit", "smaller_reader_claim_allowed"}
    _s1138_expect_keys(validation, validation_allowed, validation_allowed, "report.validation")
    _s1138_nonnegative_int(validation["human_audit_sample_n"], "report.validation.human_audit_sample_n")
    _s1138_nonnegative_int(validation["second_judge_sample_n"], "report.validation.second_judge_sample_n")
    if not isinstance(validation["smaller_reader_claim_allowed"], bool):
        raise _s1138_SensitivityValidationError("report.validation.smaller_reader_claim_allowed must be boolean")
    audit = validation["same_model_audit"]
    _s1138_expect_keys(audit, {"correlated_cell_ids", "independent_validation_claim_allowed", "finding"}, {"correlated_cell_ids", "independent_validation_claim_allowed", "finding"}, "report.validation.same_model_audit")
    if not isinstance(audit["correlated_cell_ids"], list) or not isinstance(audit["independent_validation_claim_allowed"], bool):
        raise _s1138_SensitivityValidationError("same-model audit is malformed")
    if not isinstance(audit["finding"], str) or not audit["finding"].strip():
        raise _s1138_SensitivityValidationError("same-model audit finding must be non-empty text")
    if audit["correlated_cell_ids"] and audit["independent_validation_claim_allowed"]:
        raise _s1138_SensitivityValidationError("correlated cells cannot allow independent validation claims")
    for row in value["paired_rows"]:
        _s1138_expect_keys(row, {"question_id_sha256", "question_type", "cells"}, {"question_id_sha256", "question_type", "cells"}, "report.paired_rows item")
        _s1138_digest(row["question_id_sha256"], "report.paired_rows.question_id_sha256")
        _s1138_id(row["question_type"], "report.paired_rows.question_type")
        if not isinstance(row["cells"], list) or {item.get("cell_id") for item in row["cells"] if isinstance(item, _s1138_Mapping)} != set(required):
            raise _s1138_SensitivityValidationError("paired row cells are incomplete")
        for item in row["cells"]:
            _s1138_expect_keys(item, {"cell_id", "retrieval_hit", "retrieval_rank", "answer_verdict", "abstained", "excluded", "answer_tokens_est", "independent_judge_label"}, {"cell_id", "retrieval_hit", "retrieval_rank", "answer_verdict", "abstained", "excluded", "answer_tokens_est", "independent_judge_label"}, "report.paired_rows.cells item")
            _s1138_id(item["cell_id"], "report.paired_rows.cells.cell_id")
            if not isinstance(item["retrieval_hit"], bool) or not isinstance(item["abstained"], bool) or not isinstance(item["excluded"], bool):
                raise _s1138_SensitivityValidationError("paired row booleans are invalid")
            if item["answer_verdict"] not in _s1138_VERDICTS:
                raise _s1138_SensitivityValidationError("paired row verdict is invalid")
            _s1138_nonnegative_int(item["answer_tokens_est"], "paired row answer_tokens_est")
    if not isinstance(value["category_deltas"], list) or not isinstance(value["sensitivity_table"], list) or not value["sensitivity_table"]:
        raise _s1138_SensitivityValidationError("report sensitivity projections are missing")
    expected_correlated = sorted(cell["cell_id"] for cell in cells if cell["judge"]["relation"] == "correlated-same-model")
    if audit["correlated_cell_ids"] != expected_correlated:
        raise _s1138_SensitivityValidationError("same-model audit cell set mismatch")
    if expected_correlated and audit["independent_validation_claim_allowed"]:
        raise _s1138_SensitivityValidationError("same-model audit cannot allow independent claims")
    if value["paired_rows"] != _s1138_pair_rows(cells):
        raise _s1138_SensitivityValidationError("paired projection mismatch")
    if value["category_deltas"] != _s1138_category_deltas(cells, value["baseline_cell_id"]):
        raise _s1138_SensitivityValidationError("category delta projection mismatch")
    if value["sensitivity_table"] != _s1138_sensitivity_table(cells, value["baseline_cell_id"]):
        raise _s1138_SensitivityValidationError("sensitivity table projection mismatch")
    report_base = {key: value[key] for key in allowed if key != "report_sha256"}
    if value["report_sha256"] != _s1138_sha256_text(_s1138_stable_json(report_base)):
        raise _s1138_SensitivityValidationError("report digest mismatch")
    serialized = _s1138_stable_json(value).lower()
    if any(marker in serialized for marker in _s1138_FORBIDDEN_MARKERS):
        raise _s1138_SensitivityValidationError("report contains a forbidden raw-payload marker")



def _s1138_main(argv: list[str] | None = None) -> int:
    import argparse as _s1138_argparse

    parser = _s1138_argparse.ArgumentParser(
        description="Build a provider-free LongMemEval sensitivity report"
    )
    parser.add_argument("--fixture", required=True, help="sanitized matrix fixture JSON")
    parser.add_argument("--out", required=True, help="output report JSON")
    args = parser.parse_args(argv)
    fixture_path = _s1138_Path(args.fixture)
    output_path = _s1138_Path(args.out)
    fixture = _s1138_json.loads(fixture_path.read_text(encoding="utf-8"))
    report = _s1138_build_report(fixture)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_s1138_json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("provider-free sensitivity matrix: {} cells, {} paired rows -> {}".format(len(report["cells"]), len(report["paired_rows"]), output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(_s1138_main())


_s1138_all = (
    "_s1138_SCHEMA_VERSION", "_s1138_SensitivityValidationError",
    "_s1138_build_report", "_s1138_sha256_text", "_s1138_stable_json",
    "_s1138_validate_report", "_s1138_main",
)
