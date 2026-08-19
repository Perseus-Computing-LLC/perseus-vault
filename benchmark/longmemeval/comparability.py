"""LongMemEval protocol comparability manifests and dual-lane scorecards.

The module compares score-bearing *metadata*, not prompts, answers, or memory
bodies.  It is intentionally provider-free: it never imports an SDK or makes a
network call.  A manifest is a custody/protocol commitment; a scorecard is a
bounded comparability verdict, not a new benchmark metric.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Mapping

COMPARABILITY_SCHEMA_VERSION = "perseus-vault-longmemeval-comparability/v1"
_LEGACY_SCHEMA_VERSION = "perseus-vault-longmemeval-legacy-readable/v1"
_LANES = frozenset({"official-compatible", "product-optimized"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_QID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

_TOP_FIELDS = frozenset(
    {
        "manifest_id", "schema_version", "lane", "dataset", "answerer", "judge", "prompts",
        "ingest", "retrieval", "evaluator", "provenance", "manifest_sha256",
    }
)
_DATASET_FIELDS = frozenset(
    {"name", "split", "digest_sha256", "question_count", "question_type_distribution", "scope", "exclusions"}
)
_MODEL_FIELDS = frozenset({"provider", "model", "temperature", "completion_cap", "retry_policy"})
_RETRY_FIELDS = frozenset({"max_retries", "on_error"})
_JUDGE_FIELDS = _MODEL_FIELDS | frozenset({"threshold"})
_THRESHOLD_FIELDS = frozenset({"kind", "value", "labels"})
_PROMPTS_FIELDS = frozenset({"answer", "judge"})
_PROMPT_FIELDS = frozenset({"id", "digest_sha256"})
_INGEST_FIELDS = frozenset({"shape", "memory_serialization", "context_representation"})
_RETRIEVAL_FIELDS = frozenset(
    {
        "mode", "requested_depth", "effective_depth", "context_token_budget", "context_byte_budget",
        "selection_policy", "assembly_policy",
    }
)
_EVALUATOR_FIELDS = frozenset(
    {"identity", "metric", "denominator", "excluded_cases", "failed_cases", "completion", "abstention"}
)
_PROVENANCE_FIELDS = frozenset({"state", "harness_commit", "binary_sha256", "run_sha256", "custody_sha256"})
_COMPARISON_FIELDS = (
    "lane",
    "dataset.name",
    "dataset.split",
    "dataset.digest_sha256",
    "dataset.question_count",
    "dataset.question_type_distribution",
    "dataset.scope",
    "dataset.exclusions",
    "answerer.provider",
    "answerer.model",
    "answerer.temperature",
    "answerer.completion_cap",
    "answerer.retry_policy",
    "judge.provider",
    "judge.model",
    "judge.temperature",
    "judge.completion_cap",
    "judge.retry_policy",
    "judge.threshold.kind",
    "judge.threshold.value",
    "judge.threshold.labels",
    "prompts.answer.id",
    "prompts.answer.digest_sha256",
    "prompts.judge.id",
    "prompts.judge.digest_sha256",
    "ingest.shape",
    "ingest.memory_serialization",
    "ingest.context_representation",
    "retrieval.mode",
    "retrieval.requested_depth",
    "retrieval.effective_depth",
    "retrieval.context_token_budget",
    "retrieval.context_byte_budget",
    "retrieval.selection_policy",
    "retrieval.assembly_policy",
    "evaluator.identity",
    "evaluator.metric",
    "evaluator.denominator",
    "evaluator.excluded_cases",
    "evaluator.failed_cases",
    "evaluator.completion",
    "evaluator.abstention",
    "provenance.state",
    "provenance.harness_commit",
    "provenance.binary_sha256",
)


class ManifestError(ValueError):
    """Raised when a comparability contract is missing, malformed, or unsafe."""


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ManifestError("manifest value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _keys(value: Any, expected: frozenset[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{name} must be an object")
    actual = set(value)
    missing = expected - actual
    unknown = actual - expected
    if missing:
        raise ManifestError(f"{name} missing field: {sorted(missing)[0]}")
    if unknown:
        raise ManifestError(f"{name} contains unknown field: {sorted(unknown)[0]}")


def _text(value: Any, name: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{name} must be non-empty text")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ManifestError(f"{name} contains an unsafe control character")
    if identifier and not _SAFE_ID.fullmatch(value):
        raise ManifestError(f"{name} is not a bounded identifier")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ManifestError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _commit(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ManifestError(f"{name} must be a lowercase source commit identity")
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestError(f"{name} must be an integer >= {minimum}")
    return value


def _finite_number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ManifestError(f"{name} must be finite")
    if value < minimum or value > maximum:
        raise ManifestError(f"{name} must be between {minimum} and {maximum}")
    return float(value)


def _unique_ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not _QID.fullmatch(item) for item in value):
        raise ManifestError(f"{name} must be a list of bounded question IDs")
    if len(value) != len(set(value)):
        raise ManifestError(f"{name} must not contain duplicates")
    return list(value)


def _validate_retry(value: Any, name: str) -> None:
    _keys(value, _RETRY_FIELDS, name)
    _integer(value["max_retries"], f"{name}.max_retries")
    _text(value["on_error"], f"{name}.on_error", identifier=True)


def _validate_model(value: Any, name: str, *, judge: bool) -> None:
    _keys(value, _JUDGE_FIELDS if judge else _MODEL_FIELDS, name)
    _text(value["provider"], f"{name}.provider", identifier=True)
    _text(value["model"], f"{name}.model", identifier=True)
    _finite_number(value["temperature"], f"{name}.temperature", minimum=0.0, maximum=2.0)
    _integer(value["completion_cap"], f"{name}.completion_cap", minimum=1)
    _validate_retry(value["retry_policy"], f"{name}.retry_policy")
    if judge:
        threshold = value["threshold"]
        _keys(threshold, _THRESHOLD_FIELDS, f"{name}.threshold")
        _text(threshold["kind"], f"{name}.threshold.kind", identifier=True)
        _finite_number(threshold["value"], f"{name}.threshold.value", minimum=0.0, maximum=1.0)
        labels = threshold["labels"]
        if not isinstance(labels, list) or len(labels) != 2 or any(not isinstance(item, str) for item in labels):
            raise ManifestError("judge.threshold.labels must contain exactly two labels")
        if len(set(labels)) != 2:
            raise ManifestError("judge.threshold.labels must be distinct")


def _validate_payload(value: Mapping[str, Any], *, require_digest: bool) -> None:
    expected = _TOP_FIELDS if require_digest else _TOP_FIELDS - {"schema_version", "manifest_sha256"}
    _keys(value, expected, "manifest")
    if require_digest:
        if value["schema_version"] != COMPARABILITY_SCHEMA_VERSION:
            raise ManifestError("unsupported comparability manifest schema")
        _digest(value["manifest_sha256"], "manifest_sha256")
    _text(value["manifest_id"], "manifest_id", identifier=True)
    lane = value["lane"]
    if lane not in _LANES:
        raise ManifestError("manifest lane must be official-compatible or product-optimized")

    dataset = value["dataset"]
    _keys(dataset, _DATASET_FIELDS, "dataset")
    _text(dataset["name"], "dataset.name", identifier=True)
    _text(dataset["split"], "dataset.split", identifier=True)
    _digest(dataset["digest_sha256"], "dataset.digest_sha256")
    question_count = _integer(dataset["question_count"], "dataset.question_count", minimum=1)
    distribution = dataset["question_type_distribution"]
    if not isinstance(distribution, Mapping) or not distribution:
        raise ManifestError("dataset.question_type_distribution must be non-empty")
    distribution_total = 0
    for kind, count in distribution.items():
        _text(kind, "dataset.question_type_distribution key", identifier=True)
        distribution_total += _integer(count, f"dataset.question_type_distribution.{kind}")
    if distribution_total != question_count:
        raise ManifestError("question-type distribution does not sum to question_count")
    _text(dataset["scope"], "dataset.scope", identifier=True)
    exclusions = _unique_ids(dataset["exclusions"], "dataset.exclusions")
    if len(exclusions) > question_count:
        raise ManifestError("dataset exclusions exceed question count")

    _validate_model(value["answerer"], "answerer", judge=False)
    _validate_model(value["judge"], "judge", judge=True)
    prompts = value["prompts"]
    _keys(prompts, _PROMPTS_FIELDS, "prompts")
    for prompt_name in ("answer", "judge"):
        prompt = prompts[prompt_name]
        _keys(prompt, _PROMPT_FIELDS, f"prompts.{prompt_name}")
        _text(prompt["id"], f"prompts.{prompt_name}.id", identifier=True)
        _digest(prompt["digest_sha256"], f"prompts.{prompt_name}.digest_sha256")
    if lane == "official-compatible":
        if prompts["answer"]["id"] != "longmemeval-official-cot":
            raise ManifestError("official-compatible lane requires the official CoT answer prompt")
        if prompts["judge"]["id"] != "longmemeval-official-per-type":
            raise ManifestError("official-compatible lane requires the official per-type evaluator prompt")

    ingest = value["ingest"]
    _keys(ingest, _INGEST_FIELDS, "ingest")
    for field in _INGEST_FIELDS:
        _text(ingest[field], f"ingest.{field}", identifier=True)

    retrieval = value["retrieval"]
    _keys(retrieval, _RETRIEVAL_FIELDS, "retrieval")
    _text(retrieval["mode"], "retrieval.mode", identifier=True)
    _integer(retrieval["requested_depth"], "retrieval.requested_depth", minimum=1)
    _integer(retrieval["effective_depth"], "retrieval.effective_depth", minimum=1)
    _integer(retrieval["context_token_budget"], "retrieval.context_token_budget", minimum=1)
    byte_budget = retrieval["context_byte_budget"]
    if byte_budget is not None:
        _integer(byte_budget, "retrieval.context_byte_budget", minimum=1)
    _text(retrieval["selection_policy"], "retrieval.selection_policy", identifier=True)
    _text(retrieval["assembly_policy"], "retrieval.assembly_policy", identifier=True)

    evaluator = value["evaluator"]
    _keys(evaluator, _EVALUATOR_FIELDS, "evaluator")
    _text(evaluator["identity"], "evaluator.identity", identifier=True)
    _text(evaluator["metric"], "evaluator.metric", identifier=True)
    denominator = _integer(evaluator["denominator"], "evaluator.denominator")
    excluded = _unique_ids(evaluator["excluded_cases"], "evaluator.excluded_cases")
    failed = _unique_ids(evaluator["failed_cases"], "evaluator.failed_cases")
    if set(excluded) & set(failed):
        raise ManifestError("excluded and failed evaluator cases overlap")
    if denominator + len(excluded) + len(failed) != question_count:
        raise ManifestError("evaluator denominator does not account for excluded/failed cases")
    completion = evaluator["completion"]
    if completion not in {"complete", "partial"}:
        raise ManifestError("evaluator.completion must be complete or partial")
    if completion == "complete" and (denominator != question_count or excluded or failed):
        raise ManifestError("complete evaluator cannot hide excluded or failed cases")
    if completion == "partial" and denominator == question_count and not excluded and not failed:
        raise ManifestError("partial evaluator must expose incomplete cases")
    _text(evaluator["abstention"], "evaluator.abstention", identifier=True)

    provenance = value["provenance"]
    _keys(provenance, _PROVENANCE_FIELDS, "provenance")
    if provenance["state"] != "verified":
        raise ManifestError("manifest provenance must be verified; stale/unknown artifacts are not claimable")
    _commit(provenance["harness_commit"], "provenance.harness_commit")
    _digest(provenance["binary_sha256"], "provenance.binary_sha256")
    _digest(provenance["run_sha256"], "provenance.run_sha256")
    _digest(provenance["custody_sha256"], "provenance.custody_sha256")


def build_manifest(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a manifest payload and add its canonical self-commitment."""
    if not isinstance(spec, Mapping):
        raise ManifestError("manifest specification must be an object")
    if "schema_version" in spec or "manifest_sha256" in spec:
        raise ManifestError("build_manifest accepts an unsigned payload only")
    payload = copy.deepcopy(dict(spec))
    _validate_payload(payload, require_digest=False)
    payload["schema_version"] = COMPARABILITY_SCHEMA_VERSION
    payload["manifest_sha256"] = sha256_json(payload)
    validate_manifest(payload)
    return payload


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping):
        raise ManifestError("manifest must be an object")
    _validate_payload(manifest, require_digest=True)
    expected = sha256_json({key: value for key, value in manifest.items() if key != "manifest_sha256"})
    if expected != manifest["manifest_sha256"]:
        raise ManifestError("manifest digest mismatch")


def _at_path(manifest: Mapping[str, Any], path: str) -> Any:
    current: Any = manifest
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise ManifestError(f"manifest path missing: {path}")
        current = current[part]
    return current


def _public_value(value: Any) -> Any:
    # The manifest schema contains metadata/digests only.  Copying through the
    # canonical JSON boundary makes the scorecard safe if a future caller uses
    # a mutable mapping subclass.
    return json.loads(stable_json(value))


def _reason_for(field: str) -> str:
    if field == "lane":
        return "lane-mismatch"
    if field.startswith("answerer."):
        return "answerer-mismatch"
    if field.startswith("prompts."):
        return "prompt-mismatch"
    if field.startswith("judge."):
        return "judge-mismatch"
    if field.startswith("retrieval."):
        return "retrieval-mismatch"
    if field.startswith("ingest."):
        return "context-representation-mismatch"
    if field.startswith("evaluator."):
        return "evaluator-mismatch"
    if field.startswith("dataset."):
        return "dataset-mismatch"
    return "provenance-mismatch"


def compare_manifests(reference: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deterministic field-level comparability scorecard."""
    validate_manifest(reference)
    validate_manifest(candidate)
    rows: list[dict[str, Any]] = []
    for field in _COMPARISON_FIELDS:
        left = _at_path(reference, field)
        right = _at_path(candidate, field)
        match = left == right
        rows.append(
            {
                "field": field,
                "match": match,
                "reason": None if match else _reason_for(field),
                "reference": _public_value(left),
                "candidate": _public_value(right),
            }
        )
    mismatches = [row for row in rows if not row["match"]]
    same_lane = reference["lane"] == candidate["lane"]
    base: dict[str, Any] = {
        "schema_version": COMPARABILITY_SCHEMA_VERSION,
        "reference_manifest_sha256": reference["manifest_sha256"],
        "candidate_manifest_sha256": candidate["manifest_sha256"],
        "reference_lane": reference["lane"],
        "candidate_lane": candidate["lane"],
        "lane_merge_allowed": same_lane and not mismatches,
        "claimable_like_for_like": same_lane and not mismatches,
        "disposition": "like-for-like" if same_lane and not mismatches else "not-like-for-like",
        "mismatch_count": len(mismatches),
        "mismatch_reasons": sorted({row["reason"] for row in mismatches if row["reason"]}),
        "field_results": rows,
        "raw_inputs_captured": False,
    }
    return _seal(base, "scorecard_sha256")


def _seal(value: Mapping[str, Any], digest_field: str) -> dict[str, Any]:
    result = dict(value)
    result[digest_field] = sha256_json(value)
    return result


def build_dual_lane_scorecard(lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Return separate lane summaries; never synthesize a combined score."""
    if not isinstance(lanes, Mapping) or set(lanes) != set(_LANES):
        raise ManifestError("dual-lane scorecard requires exactly both named lanes")
    manifests = {lane: lanes[lane] for lane in _LANES}
    for lane, manifest in manifests.items():
        validate_manifest(manifest)
        if manifest["lane"] != lane:
            raise ManifestError(f"manifest lane identity mismatch for {lane}")
    base: dict[str, Any] = {
        "schema_version": COMPARABILITY_SCHEMA_VERSION,
        "merged": False,
        "lanes": {
            lane: {
                "manifest_sha256": manifests[lane]["manifest_sha256"],
                "claimable": True,
                "metric_contract": manifests[lane]["evaluator"]["metric"],
                "denominator": manifests[lane]["evaluator"]["denominator"],
            }
            for lane in sorted(_LANES)
        },
        "comparison": compare_manifests(manifests["official-compatible"], manifests["product-optimized"]),
        "raw_inputs_captured": False,
    }
    # Make the no-merge rule mechanically visible at the top level too.
    return _seal(base, "scorecard_sha256")


def merge_lanes(manifests: Any) -> None:
    """Reject attempts to create one accuracy/report lane from named lanes."""
    if not isinstance(manifests, list) or len(manifests) < 2:
        raise ManifestError("lane merge requires at least two manifests and is forbidden")
    raise ManifestError("official-compatible and product-optimized lanes must remain separate")


def read_legacy_artifact(report: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect an older report without modifying or relabeling its payload."""
    if not isinstance(report, Mapping):
        raise ManifestError("legacy report must be an object")
    # Canonicalization is deliberately the only operation on the input; no
    # inferred lane/model/prompt field is written back or treated as verified.
    digest = sha256_json(report)
    return {
        "schema_version": _LEGACY_SCHEMA_VERSION,
        "status": "legacy-readable",
        "claimable_like_for_like": False,
        "reason_codes": ["missing-comparability-manifest", "protocol-fields-unverified"],
        "artifact_sha256": digest,
        "field_count": len(report),
        "raw_inputs_captured": False,
    }
