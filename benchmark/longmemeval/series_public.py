from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
SERIES_REPORT_PATH = HERE / "qa_report_cot_frozen_default_series_20260828.json"
SERIES_SCHEMA = "perseus-vault-longmemeval-frozen-default-series/v1"
SYSTEMS = ("stateless", "fullcontext", "perseus-vault", "oracle")
CATEGORIES = (
    "knowledge-update",
    "multi-session",
    "single-session-assistant",
    "single-session-preference",
    "single-session-user",
    "temporal-reasoning",
)
HEX64 = set("0123456789abcdef")
FORBIDDEN_KEYS = {"body", "response", "hypothesis", "credential", "secret", "password", "authorization", "api_key", "tool_arguments", "raw_payload"}
FORBIDDEN_SUFFIXES = ("_body", "_response", "_credential", "_secret", "_password", "_token")


class PublicSeriesError(ValueError):
    pass


def canonical_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(char not in HEX64 for char in value):
        raise PublicSeriesError(f"{name} is not a lowercase SHA-256 digest")


def _integer(value: Any, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PublicSeriesError(f"{name} is not an integer >= {minimum}")


def _rate(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise PublicSeriesError(f"{name} is not a finite rate")


def _reject(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in FORBIDDEN_KEYS or lowered.endswith(FORBIDDEN_SUFFIXES):
                raise PublicSeriesError(f"forbidden public field: {path}.{key}")
            _reject(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject(child, f"{path}[{index}]")


def _validate_score(score: Mapping[str, Any], path: str, expected_n: int) -> None:
    if set(score) != {"correct", "n", "accuracy", "by_question_type"}:
        raise PublicSeriesError(f"{path} has an unexpected shape")
    _integer(score["correct"], f"{path}.correct")
    _integer(score["n"], f"{path}.n", 1)
    if score["n"] != expected_n or score["correct"] > score["n"]:
        raise PublicSeriesError(f"{path} denominator mismatch")
    _rate(score["accuracy"], f"{path}.accuracy")
    if not math.isclose(score["accuracy"], score["correct"] / score["n"], rel_tol=0.0, abs_tol=0.00005):
        raise PublicSeriesError(f"{path}.accuracy does not match its counters")
    categories = score["by_question_type"]
    if not isinstance(categories, Mapping) or set(categories) != set(CATEGORIES):
        raise PublicSeriesError(f"{path}.by_question_type keys mismatch")
    if sum(categories[qtype]["n"] for qtype in CATEGORIES) != expected_n or sum(categories[qtype]["correct"] for qtype in CATEGORIES) != score["correct"]:
        raise PublicSeriesError(f"{path}.by_question_type totals mismatch")
    for qtype in CATEGORIES:
        row = categories[qtype]
        if set(row) != {"n", "correct", "accuracy"}:
            raise PublicSeriesError(f"{path}.by_question_type.{qtype} shape mismatch")
        _integer(row["n"], f"{path}.by_question_type.{qtype}.n", 1)
        _integer(row["correct"], f"{path}.by_question_type.{qtype}.correct")
        _rate(row["accuracy"], f"{path}.by_question_type.{qtype}.accuracy")
        if row["correct"] > row["n"] or not math.isclose(row["accuracy"], row["correct"] / row["n"], rel_tol=0.0, abs_tol=0.00005):
            raise PublicSeriesError(f"{path}.by_question_type.{qtype} rate mismatch")


def validate_public_series(series: Mapping[str, Any]) -> None:
    if series.get("schema") != SERIES_SCHEMA or series.get("status") != "accepted":
        raise PublicSeriesError("series schema/status mismatch")
    if (series.get("question_count_per_run"), series.get("quality_cells_per_run"), series.get("quality_cells_across_runs")) != (500, 2000, 6000):
        raise PublicSeriesError("series denominator mismatch")
    runs = series.get("runs")
    if not isinstance(runs, list) or len(runs) != 3 or [run.get("ordinal") for run in runs] != [1, 2, 3]:
        raise PublicSeriesError("series must contain exactly three ordered runs")
    for index, run in enumerate(runs, start=1):
        if set(run) != {"ordinal", "run_id", "source", "scores"} or not isinstance(run.get("run_id"), str) or not run["run_id"]:
            raise PublicSeriesError(f"run {index} shape mismatch")
        source = run["source"]
        if not isinstance(source, Mapping):
            raise PublicSeriesError(f"run {index} source is malformed")
        required_source = {"report_sha256", "verdict_signature_sha256", "source_commit", "binary_version", "n_attempted", "n_graded", "answer_errors", "judge_errors", "acceptance_status", "corrections"}
        if not required_source.issubset(source):
            raise PublicSeriesError(f"run {index} source is incomplete")
        for name in ("report_sha256", "verdict_signature_sha256"):
            _digest(source[name], f"runs[{index - 1}].source.{name}")
        if not isinstance(source["source_commit"], str) or len(source["source_commit"]) < 7:
            raise PublicSeriesError(f"run {index} source commit is malformed")
        _integer(source["n_attempted"], f"runs[{index - 1}].source.n_attempted")
        _integer(source["n_graded"], f"runs[{index - 1}].source.n_graded")
        if (source["n_attempted"], source["n_graded"], source["answer_errors"], source["judge_errors"]) != (2000, 2000, 0, 0):
            raise PublicSeriesError(f"run {index} source denominator/error state is not clean")
        scores = run["scores"]
        if not isinstance(scores, Mapping) or set(scores) != set(SYSTEMS):
            raise PublicSeriesError(f"run {index} score arm keys mismatch")
        for system in SYSTEMS:
            _validate_score(scores[system], f"runs[{index - 1}].scores.{system}", 500)

    aggregate = series.get("aggregate")
    if not isinstance(aggregate, Mapping) or set(aggregate) != set(SYSTEMS):
        raise PublicSeriesError("aggregate arm keys mismatch")
    for system in SYSTEMS:
        score = aggregate[system]
        required = {"runs", "pooled_correct", "pooled_n", "pooled_accuracy", "mean_run_accuracy", "by_question_type"}
        if set(score) != required:
            raise PublicSeriesError(f"aggregate.{system} shape mismatch")
        if score["runs"] != 3:
            raise PublicSeriesError(f"aggregate.{system}.runs mismatch")
        _integer(score["pooled_correct"], f"aggregate.{system}.pooled_correct")
        _integer(score["pooled_n"], f"aggregate.{system}.pooled_n", 1)
        if score["pooled_n"] != 1500:
            raise PublicSeriesError(f"aggregate.{system}.pooled_n mismatch")
        _rate(score["pooled_accuracy"], f"aggregate.{system}.pooled_accuracy")
        _rate(score["mean_run_accuracy"], f"aggregate.{system}.mean_run_accuracy")
        if not math.isclose(score["pooled_accuracy"], score["pooled_correct"] / score["pooled_n"], rel_tol=0.0, abs_tol=0.00005):
            raise PublicSeriesError(f"aggregate.{system}.pooled_accuracy mismatch")
        categories = score["by_question_type"]
        if not isinstance(categories, Mapping) or set(categories) != set(CATEGORIES):
            raise PublicSeriesError(f"aggregate.{system}.by_question_type keys mismatch")
        for qtype in CATEGORIES:
            row = categories[qtype]
            _integer(row["n"], f"aggregate.{system}.by_question_type.{qtype}.n", 1)
            _integer(row["correct"], f"aggregate.{system}.by_question_type.{qtype}.correct")
            _rate(row["accuracy"], f"aggregate.{system}.by_question_type.{qtype}.accuracy")

    protocol = series.get("common_protocol")
    expected = {
        "answer_prompt": "official-cot",
        "hypothesis_mode": "complete-response",
        "answerer_model": "gpt-4o-2024-08-06",
        "judge_model": "gpt-4o-2024-08-06",
        "temperature": 0,
        "ingest_shape": "unique-key-per-session (benchmark)",
        "max_retries": 1,
        "tpm": 25000,
        "systems": list(SYSTEMS),
    }
    if not isinstance(protocol, Mapping) or any(protocol.get(key) != value for key, value in expected.items()):
        raise PublicSeriesError("common protocol mismatch")
    if protocol.get("retrieval") != {"mode": "hybrid", "k": 10, "embedding": "bundled-onnx"}:
        raise PublicSeriesError("retrieval protocol mismatch")
    if protocol.get("context_assembly") != {"mode": "full", "assembly_k": 20, "windows_per_session": 2, "context_budget": 32768, "ledger_budget": 12000, "guidance": "none"}:
        raise PublicSeriesError("context protocol mismatch")
    _digest(protocol.get("dataset_sha256"), "common_protocol.dataset_sha256")
    _reject(series)


def load_public_series() -> dict[str, Any]:
    try:
        series = json.loads(SERIES_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicSeriesError("cannot read public series") from exc
    if not isinstance(series, dict):
        raise PublicSeriesError("public series must be an object")
    declared = series.get("content_sha256")
    body = dict(series)
    body.pop("content_sha256", None)
    if declared != canonical_digest(body):
        raise PublicSeriesError("public series content hash mismatch")
    validate_public_series(series)
    return series
