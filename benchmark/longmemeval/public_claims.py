from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
PUBLIC_MANIFEST_PATH = HERE / "accepted_frozen_default_manifest.json"
PUBLIC_REPORT_PATH = HERE / "qa_report_cot_frozen_default_20260819.json"
ACCEPTED_REPORT_SHA256 = "838f71f508b7d5eab033e7256be444164a4d7e7dcd7b33d35ae39b20510abe36"
ACCEPTED_MANIFEST_SHA256 = "38e23f5e50d6b5aa0cfa5d88c5c68387eb03eb69d88065531678dc0c1e97933d"
PUBLIC_SCHEMA = "perseus-vault-longmemeval-accepted-frozen-default-public/v1"
SOURCE_SCHEMA = "longmemeval-official-cot-frozen-default-full-acceptance/v1"
DIGEST = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
CATEGORY_TYPES = [
    "knowledge-update",
    "multi-session",
    "single-session-assistant",
    "single-session-preference",
    "single-session-user",
    "temporal-reasoning",
]
FORBIDDEN_KEYS = {"body", "response", "hypothesis", "credential", "secret", "password", "api_key", "authorization", "tool_arguments"}
MANIFEST_KEYS = {
    "abstention_subset", "accepted_report_filename", "answer_prompt", "answerer_model", "benchmark", "categories", "claim_boundary", "denominator", "hypothesis_mode", "judge_model", "judge_semantics", "preference_structured_included", "question_count", "raw_payloads_excluded", "retrieval", "run_id", "runs_2_3_started", "schema_version", "score", "source_code_commit", "source_code_tree_sha256", "source_manifest_sha256", "source_report_sha256", "split", "status", "system", "temperature",
}


class PublicClaimError(ValueError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicClaimError(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise PublicClaimError(f"{path.name} must contain an object")
    return value


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PublicClaimError(f"cannot hash {path.name}") from exc


def _id(value: Any, name: str) -> None:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise PublicClaimError(f"{name} is not a bounded identifier")


def _digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise PublicClaimError(f"{name} is not a SHA-256 digest")


def _integer(value: Any, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PublicClaimError(f"{name} is not an integer >= {minimum}")


def _rate(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise PublicClaimError(f"{name} is not a finite rate")


def _reject(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_KEYS or any(part in lowered for part in ("body", "response", "credential", "secret", "password", "authorization")):
                raise PublicClaimError(f"forbidden public field: {path}.{key}")
            _reject(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject(child, f"{path}[{index}]")


def _validate_source_report(report: Mapping[str, Any]) -> None:
    if report.get("schema") != SOURCE_SCHEMA or report.get("status") != "accepted_with_correction":
        raise PublicClaimError("accepted report schema/status mismatch")
    if report.get("raw_payloads_excluded") is not True or report.get("answer_prompt") != "official-cot" or report.get("hypothesis_mode") != "complete-response":
        raise PublicClaimError("accepted report is outside the publishable lane")
    if report.get("preference_structured_included") is not False or report.get("runs_2_3_started") is not False:
        raise PublicClaimError("accepted report crosses an excluded boundary")
    if report.get("split_size") != 500 or report.get("n_instances") != 500:
        raise PublicClaimError("accepted report question denominator mismatch")
    denominator = report.get("denominator")
    expected = {"planned_cells": 2000, "attempted_cells": 2000, "graded_cells": 2000, "answer_errors": 0, "judge_errors": 0, "unknown_or_excluded": 0}
    if not isinstance(denominator, Mapping) or any(denominator.get(k) != v for k, v in expected.items()):
        raise PublicClaimError("accepted report denominator is incomplete")
    systems = report.get("systems")
    vault = systems.get("perseus-vault") if isinstance(systems, Mapping) else None
    if not isinstance(vault, Mapping) or (vault.get("correct"), vault.get("n_graded"), vault.get("accuracy")) != (407, 500, 0.814):
        raise PublicClaimError("accepted report Vault score mismatch")
    rows = report.get("per_question")
    if not isinstance(rows, list) or len(rows) != 2000:
        raise PublicClaimError("accepted report rows are incomplete")
    row_keys = {"ans_usage", "correct", "error", "judge_usage", "question_id", "question_type", "system"}
    usage_keys = {"completion_tokens", "prompt_tokens"}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != row_keys or row.get("error") is not None or not isinstance(row.get("correct"), bool):
            raise PublicClaimError(f"accepted report row {index} is malformed")
        for field in ("question_id", "question_type", "system"):
            _id(row[field], f"per_question[{index}].{field}")
        for usage_name in ("ans_usage", "judge_usage"):
            usage = row[usage_name]
            if not isinstance(usage, Mapping) or set(usage) != usage_keys:
                raise PublicClaimError(f"accepted report row {index} usage is malformed")
            for key, value in usage.items():
                _integer(value, f"per_question[{index}].{usage_name}.{key}")
    _reject(report)


def validate_public_claim(claim: Mapping[str, Any]) -> None:
    if not isinstance(claim, Mapping) or set(claim) != MANIFEST_KEYS:
        raise PublicClaimError("public manifest has missing or unknown fields")
    if claim["schema_version"] != PUBLIC_SCHEMA:
        raise PublicClaimError("public manifest schema mismatch")
    for field in ("benchmark", "run_id", "split", "status", "system", "answer_prompt", "hypothesis_mode", "answerer_model", "judge_model"):
        _id(claim[field], field)
    if (claim["status"], claim["system"], claim["split"], claim["answer_prompt"], claim["hypothesis_mode"]) != ("accepted_with_correction", "perseus-vault", "longmemeval_s", "official-cot", "complete-response"):
        raise PublicClaimError("public manifest lane/status mismatch")
    if claim["answerer_model"] != "gpt-4o-2024-08-06" or claim["judge_model"] != "gpt-4o-2024-08-06":
        raise PublicClaimError("public manifest model mismatch")
    _id(claim["accepted_report_filename"], "accepted_report_filename")
    if claim["accepted_report_filename"] != PUBLIC_REPORT_PATH.name:
        raise PublicClaimError("public report filename mismatch")
    _digest(claim["source_report_sha256"], "source_report_sha256")
    _digest(claim["source_manifest_sha256"], "source_manifest_sha256")
    _digest(claim["source_code_tree_sha256"], "source_code_tree_sha256")
    if claim["source_report_sha256"] != ACCEPTED_REPORT_SHA256 or claim["source_manifest_sha256"] != ACCEPTED_MANIFEST_SHA256 or not COMMIT.fullmatch(claim["source_code_commit"]):
        raise PublicClaimError("public artifact commitment mismatch")
    if claim["raw_payloads_excluded"] is not True or claim["preference_structured_included"] is not False or claim["runs_2_3_started"] is not False:
        raise PublicClaimError("public manifest boundary mismatch")
    _integer(claim["question_count"], "question_count", 1)
    if claim["question_count"] != 500 or claim["temperature"] != 0:
        raise PublicClaimError("public question/decoding mismatch")
    retrieval = claim["retrieval"]
    if not isinstance(retrieval, Mapping) or set(retrieval) != {"context_mode", "context_token_budget", "effective_depth", "mode", "requested_depth"} or (retrieval["mode"], retrieval["context_mode"]) != ("hybrid", "full"):
        raise PublicClaimError("public retrieval protocol mismatch")
    for field in ("requested_depth", "effective_depth", "context_token_budget"):
        _integer(retrieval[field], f"retrieval.{field}", 1)
    if (retrieval["requested_depth"], retrieval["effective_depth"], retrieval["context_token_budget"]) != (10, 10, 32768):
        raise PublicClaimError("public depth/budget mismatch")
    score = claim["score"]
    if not isinstance(score, Mapping) or set(score) != {"accuracy", "correct", "denominator"} or (score["correct"], score["denominator"], score["accuracy"]) != (407, 500, 0.814):
        raise PublicClaimError("public score mismatch")
    _integer(score["correct"], "score.correct")
    _integer(score["denominator"], "score.denominator", 1)
    _rate(score["accuracy"], "score.accuracy")
    categories = claim["categories"]
    if not isinstance(categories, list) or [row.get("question_type") for row in categories] != CATEGORY_TYPES:
        raise PublicClaimError("public category order mismatch")
    for index, row in enumerate(categories):
        if not isinstance(row, Mapping) or set(row) != {"accuracy", "correct", "n", "question_type"}:
            raise PublicClaimError(f"public category {index} is malformed")
        _id(row["question_type"], f"categories[{index}].question_type")
        _integer(row["n"], f"categories[{index}].n", 1)
        _integer(row["correct"], f"categories[{index}].correct")
        _rate(row["accuracy"], f"categories[{index}].accuracy")
        if row["correct"] > row["n"] or not math.isclose(row["accuracy"], row["correct"] / row["n"], rel_tol=0.0, abs_tol=1e-15):
            raise PublicClaimError(f"public category {index} rate mismatch")
    if sum(row["n"] for row in categories) != 500 or sum(row["correct"] for row in categories) != 407:
        raise PublicClaimError("public category totals mismatch")
    abstention = claim["abstention_subset"]
    if not isinstance(abstention, Mapping) or set(abstention) != {"accuracy", "n"}:
        raise PublicClaimError("public abstention subset is malformed")
    _integer(abstention["n"], "abstention_subset.n", 1)
    _rate(abstention["accuracy"], "abstention_subset.accuracy")
    denominator = claim["denominator"]
    expected = {"planned_cells": 2000, "attempted_cells": 2000, "graded_cells": 2000, "answer_errors": 0, "judge_errors": 0, "unknown_or_excluded": 0}
    if not isinstance(denominator, Mapping) or set(denominator) != set(expected) or any(denominator.get(k) != v for k, v in expected.items()):
        raise PublicClaimError("public denominator mismatch")
    for key in expected:
        _integer(denominator[key], f"denominator.{key}")
    if not isinstance(claim["claim_boundary"], str) or not claim["claim_boundary"].strip():
        raise PublicClaimError("claim boundary is missing")
    _reject(claim)


def load_public_claim() -> dict[str, Any]:
    claim = _read(PUBLIC_MANIFEST_PATH)
    validate_public_claim(claim)
    if _hash(PUBLIC_REPORT_PATH) != claim["source_report_sha256"]:
        raise PublicClaimError("accepted report bytes do not match its commitment")
    _validate_source_report(_read(PUBLIC_REPORT_PATH))
    return claim
