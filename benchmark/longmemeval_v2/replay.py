"""Provider-free LongMemEval-V2 readiness replay and artifact custody."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from .adapter import ADAPTER_SCHEMA_VERSION, LongMemEvalV2VaultMemory

REPLAY_SCHEMA_VERSION = "perseus-vault-longmemeval-v2-provider-free-replay/v1"
MANIFEST_SCHEMA_VERSION = "perseus-vault-longmemeval-v2-provider-free-manifest/v1"
INVENTORY_SCHEMA_VERSION = "perseus-vault-longmemeval-v2-artifact-inventory/v1"
FIXTURE_SCHEMA_VERSION = "perseus-vault-longmemeval-v2-synthetic-fixture/v1"
V2_REPOSITORY = "https://github.com/xiaowu0162/LongMemEval-V2"
V2_REVISION = "2cc8c540bdb87fe6761629b585e727e1c4704520"
V2_HARNESS_PATH = "evaluation/harness.py"
V2_BACKEND_PATH = "memory_modules/memory.py"
# SHA-256 of the exact raw harness file at V2_REVISION, captured during the
# pinned-source review. The source is configuration evidence only; it is not
# downloaded or executed by this provider-free lane.
V2_HARNESS_SOURCE_SHA256 = "93fe5855a74ad46d7e8b489cebac24de38a9b30ba7ec1de2dd8708bd4aeebdb6"
ABILITY_NAMES = (
    "static_state_recall",
    "dynamic_state_tracking",
    "workflow_knowledge",
    "environment_gotchas",
    "premise_awareness",
)
FORBIDDEN_REPORT_MARKERS = frozenset(
    {
        "question_id",
        "question_type",
        "answer_session_ids",
        "gold_answer",
        "evaluator_metadata",
        "hidden_label",
        "raw_prompt",
        "provider_response",
        "customer_data",
        "api_key",
        "credential",
    }
)
_ALLOWED_CASE_ADAPTER_KEYS = frozenset(
    {"scope", "available", "max_results", "max_text_chars", "max_total_text_chars", "max_image_items"}
)
_GOLD_BLIND_INPUT_FIELDS = frozenset(
    {
        "question_id",
        "question_type",
        "answer_session_ids",
        "gold_answer",
        "evaluator_metadata",
        "evaluator_config",
        "hidden_label",
        "gold",
        "answer",
    }
)


class ReplayContractError(ValueError):
    """Raised when a provider-free replay or artifact is invalid."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReplayContractError("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReplayContractError(f"unable to hash artifact {path}") from exc


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    head = result.stdout.strip()
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head):
        raise ReplayContractError("Vault source HEAD is not a full lowercase commit SHA")
    return head


def _relative_repo_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ReplayContractError(f"artifact is outside repository root: {path}") from exc


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayContractError(f"invalid synthetic fixture: {path}") from exc
    if not isinstance(fixture, dict) or fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ReplayContractError("unsupported synthetic fixture schema")
    cases = fixture.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ReplayContractError("synthetic fixture cases must be a non-empty list")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ReplayContractError(f"fixture case {index} is not an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ReplayContractError(f"fixture case {index} has an invalid or duplicate case_id")
        seen.add(case_id)
        if case.get("ability") not in ABILITY_NAMES:
            raise ReplayContractError(f"fixture case {case_id} has an unsupported ability")
        if case.get("domain") not in {"web", "enterprise"}:
            raise ReplayContractError(f"fixture case {case_id} has an unsupported domain")
        trajectories = case.get("trajectories")
        if not isinstance(trajectories, list) or not trajectories:
            raise ReplayContractError(f"fixture case {case_id} needs at least one trajectory")
        if not isinstance(case.get("query"), str):
            raise ReplayContractError(f"fixture case {case_id} query must be text")
        if not isinstance(case.get("expected"), dict):
            raise ReplayContractError(f"fixture case {case_id} needs expected metadata")
    return fixture


def _adapter_params(base: Mapping[str, Any], case: Mapping[str, Any], repo_root: Path) -> dict[str, Any]:
    params = dict(base)
    override = case.get("adapter", {})
    if not isinstance(override, Mapping):
        raise ReplayContractError(f"fixture case {case['case_id']} adapter override is not an object")
    unknown = set(override) - _ALLOWED_CASE_ADAPTER_KEYS
    if unknown:
        raise ReplayContractError(f"fixture case {case['case_id']} has unknown adapter keys")
    params.update(override)
    params["allowed_image_root"] = str(repo_root)
    return params


def _check_expected(case: Mapping[str, Any], items: list[dict[str, str]], diagnostic: Mapping[str, Any]) -> bool:
    expected = case["expected"]
    if diagnostic.get("status") != expected.get("status"):
        return False
    if expected.get("reason") is not None and diagnostic.get("reason") != expected["reason"]:
        return False
    text_values = [item["value"] for item in items if item.get("type") == "text"]
    image_values = [item["value"] for item in items if item.get("type") == "image"]
    if len(text_values) < int(expected.get("min_text_items", 0)):
        return False
    if len(text_values) > int(expected.get("max_text_items", 1_000_000)):
        return False
    if len(image_values) < int(expected.get("min_image_items", 0)):
        return False
    if expected.get("conflicts_visible") is not None and diagnostic.get("conflicts_visible") != expected["conflicts_visible"]:
        return False
    contains = expected.get("contains")
    if contains is not None and not any(contains in value for value in text_values):
        return False
    not_contains = expected.get("not_contains")
    if not_contains is not None and any(not_contains in value for value in text_values):
        return False
    if expected.get("event_order") is not None:
        order: list[int] = []
        for value in text_values:
            for part in value.split("; "):
                if part.startswith("event_index="):
                    order.append(int(part.split("=", 1)[1]))
                    break
        if order[: len(expected["event_order"])] != expected["event_order"]:
            return False
    return True


def _safe_item_projection(item: Mapping[str, str]) -> dict[str, Any]:
    item_type = item.get("type")
    value = item.get("value")
    if item_type not in {"text", "image"} or not isinstance(value, str) or not value:
        raise ReplayContractError("adapter emitted an invalid V2 context item")
    projected: dict[str, Any] = {
        "type": item_type,
        "value_sha256": _text_sha256(value),
        "value_chars": len(value),
    }
    if item_type == "image":
        path = Path(value)
        if not path.is_file():
            raise ReplayContractError("adapter emitted a missing image path")
        projected["image_sha256"] = file_sha256(path)
    return projected


def _strip_benchmark_metadata(value: Any) -> Any:
    """Keep benchmark labels in the outer harness, never in adapter inputs."""
    if isinstance(value, Mapping):
        return {
            key: _strip_benchmark_metadata(child)
            for key, child in value.items()
            if str(key).lower() not in _GOLD_BLIND_INPUT_FIELDS
        }
    if isinstance(value, list):
        return [_strip_benchmark_metadata(child) for child in value]
    return value


def replay_fixture(case: Mapping[str, Any], adapter: LongMemEvalV2VaultMemory) -> dict[str, Any]:
    """Execute one case with only V2 boundary arguments entering the adapter."""
    trajectories = case["trajectories"]
    for trajectory in trajectories:
        if not isinstance(trajectory, Mapping):
            raise ReplayContractError(f"case {case['case_id']} contains a malformed trajectory")
        adapter.insert(_strip_benchmark_metadata(trajectory))
    query = case["query"]
    query_image = case.get("query_image")
    if query_image is not None and not isinstance(query_image, str):
        raise ReplayContractError(f"case {case['case_id']} query_image must be text or null")
    items = adapter.query(query, query_image)
    diagnostic = adapter.post_query_hook(query=query, query_image=query_image, memory_context=items)
    if not isinstance(diagnostic, Mapping):
        raise ReplayContractError("adapter diagnostic is not an object")
    expected_match = _check_expected(case, items, diagnostic)
    item_projections = [_safe_item_projection(item) for item in items]
    return {
        "case_id": case["case_id"],
        "ability": case["ability"],
        "domain": case["domain"],
        "expected_match": expected_match,
        "retrieval": {
            "status": diagnostic["status"],
            "reason": diagnostic["reason"],
            "text_items": diagnostic["text_items"],
            "image_items": diagnostic["image_items"],
            "evidence": item_projections,
        },
        "context": {
            "bounded": diagnostic["bounded"],
            "text_items": diagnostic["text_items"],
            "image_items": diagnostic["image_items"],
        },
        "instrumentation": {
            "query_sha256": diagnostic["query_sha256"],
            "query_image_sha256": diagnostic.get("query_image_sha256"),
            "excluded": diagnostic["excluded"],
            "conflicts_visible": diagnostic["conflicts_visible"],
        },
    }


def _scorecard(rows: list[dict[str, Any]], key: str, values: tuple[str, ...]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row)
    result: dict[str, Any] = {}
    for value in values:
        subset = grouped.get(value, [])
        status_counts = Counter(row["retrieval"]["status"] for row in subset)
        result[value] = {
            "case_count": len(subset),
            "provider_free_ready_cases": sum(bool(row["expected_match"]) for row in subset),
            "status_counts": {name: status_counts[name] for name in sorted(status_counts)},
            "context_bounded_cases": sum(bool(row["context"]["bounded"]) for row in subset),
            "answer_accuracy": {"status": "not_measured", "value": None},
            "answerer_calls": 0,
            "judge_calls": 0,
        }
    return result


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RETRIEVAL_STATUSES = frozenset({"complete", "partial", "degraded", "abstained", "unavailable"})


def _require_keys(value: Any, expected: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ReplayContractError(f"{name} keys do not match the contract")
    return value


def _require_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ReplayContractError(f"{name} must be a lowercase SHA-256 digest")


def _require_nonnegative_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayContractError(f"{name} must be a non-negative integer")


def _require_nonempty_text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ReplayContractError(f"{name} must be non-empty text")


def _status_count_total(value: Any, name: str) -> int:
    if not isinstance(value, Mapping):
        raise ReplayContractError(f"{name} must be an object")
    total = 0
    for status, count in value.items():
        if status not in _RETRIEVAL_STATUSES or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ReplayContractError(f"{name} contains an invalid status count")
        total += count
    return total


def _validate_case_row(row: Any, index: int) -> None:
    name = f"cases[{index}]"
    expected_case_keys = {"case_id", "ability", "domain", "expected_match", "retrieval", "context", "instrumentation"}
    case = _require_keys(row, expected_case_keys, name)
    _require_nonempty_text(case["case_id"], f"{name}.case_id")
    if not isinstance(case["ability"], str) or not isinstance(case["domain"], str) or case["ability"] not in ABILITY_NAMES or case["domain"] not in {"web", "enterprise"}:
        raise ReplayContractError(f"{name} has an unsupported ability or domain")
    if not isinstance(case["expected_match"], bool):
        raise ReplayContractError(f"{name}.expected_match must be boolean")

    retrieval = _require_keys(
        case["retrieval"],
        {"status", "reason", "text_items", "image_items", "evidence"},
        f"{name}.retrieval",
    )
    if retrieval["status"] not in _RETRIEVAL_STATUSES:
        raise ReplayContractError(f"{name}.retrieval.status is unsupported")
    _require_nonempty_text(retrieval["reason"], f"{name}.retrieval.reason")
    _require_nonnegative_int(retrieval["text_items"], f"{name}.retrieval.text_items")
    _require_nonnegative_int(retrieval["image_items"], f"{name}.retrieval.image_items")
    if not isinstance(retrieval["evidence"], list):
        raise ReplayContractError(f"{name}.retrieval.evidence must be a list")
    if len(retrieval["evidence"]) != retrieval["text_items"] + retrieval["image_items"]:
        raise ReplayContractError(f"{name}.retrieval evidence count is inconsistent")
    for item_index, item in enumerate(retrieval["evidence"]):
        item_name = f"{name}.retrieval.evidence[{item_index}]"
        item_map = _require_keys(item, {"type", "value_sha256", "value_chars"} | ({"image_sha256"} if isinstance(item, Mapping) and item.get("type") == "image" else set()), item_name)
        if item_map["type"] not in {"text", "image"}:
            raise ReplayContractError(f"{item_name}.type is unsupported")
        _require_digest(item_map["value_sha256"], f"{item_name}.value_sha256")
        _require_nonnegative_int(item_map["value_chars"], f"{item_name}.value_chars")
        if item_map["value_chars"] == 0:
            raise ReplayContractError(f"{item_name}.value_chars must be positive")
        if item_map["type"] == "image":
            _require_digest(item_map["image_sha256"], f"{item_name}.image_sha256")

    context = _require_keys(
        case["context"],
        {"bounded", "text_items", "image_items"},
        f"{name}.context",
    )
    if not isinstance(context["bounded"], bool):
        raise ReplayContractError(f"{name}.context.bounded must be boolean")
    _require_nonnegative_int(context["text_items"], f"{name}.context.text_items")
    _require_nonnegative_int(context["image_items"], f"{name}.context.image_items")
    if context["text_items"] != retrieval["text_items"] or context["image_items"] != retrieval["image_items"]:
        raise ReplayContractError(f"{name}.context counts do not match retrieval counts")

    instrumentation = _require_keys(
        case["instrumentation"],
        {"query_sha256", "query_image_sha256", "excluded", "conflicts_visible"},
        f"{name}.instrumentation",
    )
    _require_digest(instrumentation["query_sha256"], f"{name}.instrumentation.query_sha256")
    if instrumentation["query_image_sha256"] is not None:
        _require_digest(instrumentation["query_image_sha256"], f"{name}.instrumentation.query_image_sha256")
    excluded = _require_keys(
        instrumentation["excluded"],
        {"scope", "lifecycle", "superseded", "missing_images"},
        f"{name}.instrumentation.excluded",
    )
    for excluded_name, excluded_value in excluded.items():
        _require_nonnegative_int(excluded_value, f"{name}.instrumentation.excluded.{excluded_name}")
    _require_nonnegative_int(instrumentation["conflicts_visible"], f"{name}.instrumentation.conflicts_visible")


def validate_replay_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping) or report.get("schema_version") != REPLAY_SCHEMA_VERSION:
        raise ReplayContractError("unsupported replay report schema")
    required = {
        "schema_version", "manifest_sha256", "run_signature_sha256", "case_count", "cases",
        "ability_scorecard", "domain_scorecard", "metrics", "provider_calls", "network_calls",
        "model_calls", "judge_calls", "paid_spend_usd", "offline", "claim_boundary",
    }
    if set(report) != required:
        raise ReplayContractError(f"replay report keys do not match the contract: {sorted(set(report) ^ required)}")
    for name in ("provider_calls", "network_calls", "model_calls", "judge_calls"):
        _require_nonnegative_int(report[name], name)
        if report[name] != 0:
            raise ReplayContractError(f"{name} must be zero")
    if isinstance(report["paid_spend_usd"], bool) or not isinstance(report["paid_spend_usd"], (int, float)) or report["paid_spend_usd"] != 0 or report["offline"] is not True:
        raise ReplayContractError("provider-free report has nonzero spend or is not offline")
    _require_digest(report["manifest_sha256"], "manifest_sha256")
    _require_digest(report["run_signature_sha256"], "run_signature_sha256")
    if not isinstance(report["cases"], list):
        raise ReplayContractError("cases must be a list")
    if isinstance(report["case_count"], bool) or not isinstance(report["case_count"], int):
        raise ReplayContractError("case_count must be an integer")
    if report["case_count"] != len(report["cases"]) or report["case_count"] <= 0:
        raise ReplayContractError("case_count does not match cases")
    for index, row in enumerate(report["cases"]):
        _validate_case_row(row, index)
    case_ids = [row["case_id"] for row in report["cases"]]
    if len(case_ids) != len(set(case_ids)):
        raise ReplayContractError("case IDs must be unique")
    if not all(row["expected_match"] for row in report["cases"]):
        raise ReplayContractError("provider-free replay contains an unexpected case result")
    if not isinstance(report["ability_scorecard"], Mapping) or not isinstance(report["domain_scorecard"], Mapping):
        raise ReplayContractError("scorecards must be objects")
    if set(report["ability_scorecard"]) != set(ABILITY_NAMES):
        raise ReplayContractError("five-ability scorecard is incomplete")
    if set(report["domain_scorecard"]) != {"web", "enterprise"}:
        raise ReplayContractError("domain scorecard is incomplete")
    scorecard_keys = {"case_count", "provider_free_ready_cases", "status_counts", "context_bounded_cases", "answer_accuracy", "answerer_calls", "judge_calls"}
    for scorecard_name, scorecard in list(report["ability_scorecard"].items()) + list(report["domain_scorecard"].items()):
        values = _require_keys(scorecard, scorecard_keys, f"scorecard.{scorecard_name}")
        _require_nonnegative_int(values["case_count"], f"scorecard.{scorecard_name}.case_count")
        _require_nonnegative_int(values["provider_free_ready_cases"], f"scorecard.{scorecard_name}.provider_free_ready_cases")
        _require_nonnegative_int(values["context_bounded_cases"], f"scorecard.{scorecard_name}.context_bounded_cases")
        if values["case_count"] == 0 or values["provider_free_ready_cases"] != values["case_count"] or values["context_bounded_cases"] != values["case_count"]:
            raise ReplayContractError(f"scorecard.{scorecard_name} is incomplete")
        if _status_count_total(values["status_counts"], f"scorecard.{scorecard_name}.status_counts") != values["case_count"]:
            raise ReplayContractError(f"scorecard.{scorecard_name}.status_counts is inconsistent")
        answer_accuracy = _require_keys(values["answer_accuracy"], {"status", "value"}, f"scorecard.{scorecard_name}.answer_accuracy")
        if answer_accuracy["status"] != "not_measured" or answer_accuracy["value"] is not None:
            raise ReplayContractError(f"scorecard.{scorecard_name} reports answer accuracy")
        _require_nonnegative_int(values["answerer_calls"], f"scorecard.{scorecard_name}.answerer_calls")
        _require_nonnegative_int(values["judge_calls"], f"scorecard.{scorecard_name}.judge_calls")
        if values["answerer_calls"] != 0 or values["judge_calls"] != 0:
            raise ReplayContractError(f"scorecard.{scorecard_name} reports model activity")
    metrics = report["metrics"]
    if not isinstance(metrics, Mapping) or set(metrics) != {"retrieval", "context", "answer", "cost", "instrumentation"}:
        raise ReplayContractError("metrics are not separated into the required surfaces")
    retrieval_metrics = _require_keys(metrics["retrieval"], {"case_count", "provider_free_ready_cases", "evidence_cases", "status_counts"}, "metrics.retrieval")
    for metric_name in ("case_count", "provider_free_ready_cases", "evidence_cases"):
        _require_nonnegative_int(retrieval_metrics[metric_name], f"metrics.retrieval.{metric_name}")
    if retrieval_metrics["case_count"] != report["case_count"] or retrieval_metrics["provider_free_ready_cases"] != report["case_count"] or _status_count_total(retrieval_metrics["status_counts"], "metrics.retrieval.status_counts") != report["case_count"]:
        raise ReplayContractError("metrics.retrieval counts are inconsistent")
    context_metrics = _require_keys(metrics["context"], {"bounded_cases", "text_items", "image_items", "memory_context_max_tokens"}, "metrics.context")
    for metric_name in ("bounded_cases", "text_items", "image_items", "memory_context_max_tokens"):
        _require_nonnegative_int(context_metrics[metric_name], f"metrics.context.{metric_name}")
    if context_metrics["bounded_cases"] != report["case_count"] or context_metrics["memory_context_max_tokens"] <= 0:
        raise ReplayContractError("metrics.context is incomplete")
    answer_metrics = _require_keys(metrics["answer"], {"status", "accuracy", "answerer_calls", "judge_calls"}, "metrics.answer")
    _require_nonnegative_int(answer_metrics["answerer_calls"], "metrics.answer.answerer_calls")
    _require_nonnegative_int(answer_metrics["judge_calls"], "metrics.answer.judge_calls")
    if answer_metrics["status"] != "not_measured" or answer_metrics["accuracy"] is not None or answer_metrics["answerer_calls"] != 0 or answer_metrics["judge_calls"] != 0:
        raise ReplayContractError("metrics.answer must be explicitly unmeasured")
    cost_metrics = _require_keys(metrics["cost"], {"provider_calls", "network_calls", "model_calls", "judge_calls", "paid_spend_usd"}, "metrics.cost")
    for cost_name in ("provider_calls", "network_calls", "model_calls", "judge_calls"):
        _require_nonnegative_int(cost_metrics[cost_name], f"metrics.cost.{cost_name}")
    if isinstance(cost_metrics["paid_spend_usd"], bool) or not isinstance(cost_metrics["paid_spend_usd"], (int, float)):
        raise ReplayContractError("metrics.cost.paid_spend_usd must be numeric")
    if any(cost_metrics[name] != 0 for name in ("provider_calls", "network_calls", "model_calls", "judge_calls", "paid_spend_usd")):
        raise ReplayContractError("metrics.cost reports activity or spend")
    instrumentation_metrics = _require_keys(metrics["instrumentation"], {"unavailable_cases", "abstained_cases", "conflicting_cases", "query_digests_only"}, "metrics.instrumentation")
    for metric_name in ("unavailable_cases", "abstained_cases", "conflicting_cases"):
        _require_nonnegative_int(instrumentation_metrics[metric_name], f"metrics.instrumentation.{metric_name}")
    if not isinstance(instrumentation_metrics["query_digests_only"], bool):
        raise ReplayContractError("metrics.instrumentation.query_digests_only must be boolean")
    claim_boundary = _require_keys(report["claim_boundary"], {"supported", "not_supported"}, "claim_boundary")
    for field in ("supported", "not_supported"):
        if not isinstance(claim_boundary[field], list) or not claim_boundary[field] or any(not isinstance(item, str) or not item for item in claim_boundary[field]):
            raise ReplayContractError(f"claim_boundary.{field} must be a non-empty text list")
    unsigned = dict(report)
    signature = unsigned.pop("run_signature_sha256")
    if not isinstance(signature, str) or signature != canonical_sha256(unsigned):
        raise ReplayContractError("run signature does not match the report")
    serialized = json.dumps(report, sort_keys=True, ensure_ascii=True).lower()
    for marker in FORBIDDEN_REPORT_MARKERS:
        if marker.lower() in serialized:
            raise ReplayContractError(f"forbidden report marker present: {marker}")



def _build_manifest(fixture_path: Path, config_path: Path, repo_root: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    schema_path = repo_root / "src" / "schema.rs"
    if not schema_path.is_file():
        raise ReplayContractError("Vault schema source is missing")
    binary_candidates = [repo_root / "target" / "release" / "perseus-vault", repo_root / "target" / "debug" / "perseus-vault"]
    binary_path = next((path for path in binary_candidates if path.is_file()), None)
    if binary_path is None:
        binary_status = "not_built_provider_free_adapter_only"
        binary_sha256 = _text_sha256("perseus-vault-binary-not-built")
        binary_name = None
    else:
        binary_status = "built_not_executed_provider_free_adapter_only"
        binary_sha256 = file_sha256(binary_path)
        binary_name = _relative_repo_path(binary_path, repo_root)
    adapter_config = config["adapter"]
    prompt_config = config["prompts"]
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "benchmark": {
            "name": "LongMemEval-V2",
            "repository": V2_REPOSITORY,
            "revision": V2_REVISION,
            "backend_path": V2_BACKEND_PATH,
            "harness_path": V2_HARNESS_PATH,
            "harness_revision_sha256": _text_sha256(V2_REVISION + ":" + V2_HARNESS_PATH),
            "harness_source_sha256": V2_HARNESS_SOURCE_SHA256,
        },
        "dataset": {
            "revision": "synthetic-provider-free-v1",
            "path": _relative_repo_path(fixture_path, repo_root),
            "sha256": file_sha256(fixture_path),
        },
        "vault": {
            "source_commit": _git_head(repo_root),
            "binary_status": binary_status,
            "binary_path": binary_name,
            "binary_sha256": binary_sha256,
            "schema_path": _relative_repo_path(schema_path, repo_root),
            "schema_sha256": file_sha256(schema_path),
        },
        "adapter": {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "name": adapter_config["name"],
            "version": adapter_config["version"],
            "configuration_sha256": canonical_sha256(adapter_config),
            "configuration_path": _relative_repo_path(config_path, repo_root),
        },
        "prompts": {
            "reader": {
                "identifier": prompt_config["reader"]["identifier"],
                "sha256": _text_sha256(prompt_config["reader"]["identifier"]),
            },
            "judge": {
                "identifier": prompt_config["judge"]["identifier"],
                "sha256": _text_sha256(prompt_config["judge"]["identifier"]),
            },
        },
        "reader": copy.deepcopy(config["reader"]),
        "judge": copy.deepcopy(config["judge"]),
        "reader_config_sha256": canonical_sha256(config["reader"]),
        "judge_config_sha256": canonical_sha256(config["judge"]),
        "token_budgets": copy.deepcopy(config["token_budgets"]),
        "token_budgets_sha256": canonical_sha256(config["token_budgets"]),
        "execution": copy.deepcopy(config["execution"]),
        "execution_sha256": canonical_sha256(config["execution"]),
        "offline": {
            "offline": True,
            "provider_calls": 0,
            "network_calls": 0,
            "model_calls": 0,
            "judge_calls": 0,
            "paid_spend_usd": 0,
        },
        "generated_artifacts": ["manifest.json", "replay_report.json", "replay_signature.txt", "artifact_inventory.json"],
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def run_replay(fixture_path: Path, outdir: Path, *, repo_root: Path) -> dict[str, Any]:
    fixture_path = fixture_path.resolve()
    repo_root = repo_root.resolve()
    outdir = outdir.resolve()
    config_path = fixture_path.parent.parent / "provider_free_config.json"
    fixture = load_fixture(fixture_path)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    manifest = _build_manifest(fixture_path, config_path, repo_root, config)
    manifest_path = outdir / "manifest.json"
    _write_json(manifest_path, manifest)
    manifest_sha = file_sha256(manifest_path)

    base_params = dict(config["adapter"])
    base_params.pop("name", None)
    base_params.pop("version", None)
    rows: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        adapter = LongMemEvalV2VaultMemory(_adapter_params(base_params, case, repo_root))
        rows.append(replay_fixture(case, adapter))

    statuses = Counter(row["retrieval"]["status"] for row in rows)
    report: dict[str, Any] = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "manifest_sha256": manifest_sha,
        "case_count": len(rows),
        "cases": rows,
        "ability_scorecard": _scorecard(rows, "ability", ABILITY_NAMES),
        "domain_scorecard": _scorecard(rows, "domain", ("web", "enterprise")),
        "metrics": {
            "retrieval": {
                "case_count": len(rows),
                "provider_free_ready_cases": sum(bool(row["expected_match"]) for row in rows),
                "evidence_cases": sum(row["retrieval"]["text_items"] + row["retrieval"]["image_items"] > 0 for row in rows),
                "status_counts": {name: statuses[name] for name in sorted(statuses)},
            },
            "context": {
                "bounded_cases": sum(bool(row["context"]["bounded"]) for row in rows),
                "text_items": sum(row["context"]["text_items"] for row in rows),
                "image_items": sum(row["context"]["image_items"] for row in rows),
                "memory_context_max_tokens": config["token_budgets"]["memory_context_max_tokens"],
            },
            "answer": {
                "status": "not_measured",
                "accuracy": None,
                "answerer_calls": 0,
                "judge_calls": 0,
            },
            "cost": {
                "provider_calls": 0,
                "network_calls": 0,
                "model_calls": 0,
                "judge_calls": 0,
                "paid_spend_usd": 0,
            },
            "instrumentation": {
                "unavailable_cases": statuses.get("unavailable", 0),
                "abstained_cases": statuses.get("abstained", 0),
                "conflicting_cases": sum(row["instrumentation"]["conflicts_visible"] > 0 for row in rows),
                "query_digests_only": True,
            },
        },
        "provider_calls": 0,
        "network_calls": 0,
        "model_calls": 0,
        "judge_calls": 0,
        "paid_spend_usd": 0,
        "offline": True,
        "claim_boundary": {
            "supported": [
                "provider-free V2 insert/query adapter contract",
                "governed identity, scope, lifecycle, conflict, supersession, and bounded evidence projection",
                "deterministic preparation and replay custody",
            ],
            "not_supported": [
                "answer accuracy or judge quality",
                "full-split LongMemEval-V2 efficacy",
                "customer or production efficacy",
                "cross-model or cross-provider superiority",
            ],
        },
    }
    report["run_signature_sha256"] = canonical_sha256(report)
    validate_replay_report(report)
    report_path = outdir / "replay_report.json"
    _write_json(report_path, report)
    signature_path = outdir / "replay_signature.txt"
    signature_path.write_text(report["run_signature_sha256"] + "\n", encoding="ascii")

    inventory_files = [
        ("manifest.json", manifest_path),
        ("replay_report.json", report_path),
        ("replay_signature.txt", signature_path),
        (_relative_repo_path(fixture_path, repo_root), fixture_path),
        (_relative_repo_path(config_path, repo_root), config_path),
        (_relative_repo_path(fixture_path.parent / "synthetic-screenshot.png", repo_root), fixture_path.parent / "synthetic-screenshot.png"),
        (_relative_repo_path(repo_root / "src" / "schema.rs", repo_root), repo_root / "src" / "schema.rs"),
    ]
    binary_candidates = [repo_root / "target" / "release" / "perseus-vault", repo_root / "target" / "debug" / "perseus-vault"]
    binary_path = next((path for path in binary_candidates if path.is_file()), None)
    if binary_path is not None:
        inventory_files.append((_relative_repo_path(binary_path, repo_root), binary_path))
    inventory = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "provider_free": True,
        "raw_inputs_captured": False,
        "generated_artifacts": [{"path": name, "sha256": file_sha256(path)} for name, path in inventory_files],
    }
    inventory_path = outdir / "artifact_inventory.json"
    _write_json(inventory_path, inventory)
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "report_path": str(report_path),
        "report_sha256": file_sha256(report_path),
        "signature_path": str(signature_path),
        "run_signature_sha256": report["run_signature_sha256"],
        "inventory_path": str(inventory_path),
        "inventory_sha256": file_sha256(inventory_path),
        "case_count": len(rows),
        "provider_calls": 0,
        "network_calls": 0,
        "model_calls": 0,
        "judge_calls": 0,
        "paid_spend_usd": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the provider-free LongMemEval-V2 readiness replay")
    default_fixture = Path(__file__).resolve().parent / "fixtures" / "synthetic_v2.json"
    default_repo_root = Path(__file__).resolve().parents[2]
    parser.add_argument("--fixture", type=Path, default=default_fixture)
    parser.add_argument("--outdir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    args = parser.parse_args()
    result = run_replay(args.fixture, args.outdir, repo_root=args.repo_root)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
