#!/usr/bin/env python3
"""Provider-free LongMemEval failure attribution gate (#1132).

The gate joins the accepted frozen-default verdict projection with an existing
sanitized retrieval replay.  It never calls an answerer, judge, retriever
service, or network endpoint.  The optional local dataset is used only to
compute deterministic source/provenance/temporal features; the emitted report
contains bounded IDs, ranks, flags, reason codes, counts, budgets, and hashes.

This is an attribution surface, not a replacement QA run.  The frozen default
answering path is not imported or modified here.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


_ATTR_REPLAY_ROOT = Path(__file__).resolve().parents[2]
if str(_ATTR_REPLAY_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATTR_REPLAY_ROOT))
from benchmark.package.common.replay import (  # noqa: E402
    ReplayValidationError as _ATTR_ReplayValidationError,
    sha256_text as _ATTR_replay_sha256_text,
    validate_envelope as _ATTR_validate_replay_envelope,
)


_ATTR_SCHEMA = "longmemeval-failure-attribution/v1"
_ATTR_FIXTURE_SCHEMA = "longmemeval-failure-attribution-fixture/v1"
_ATTR_BASELINE_SCHEMA = "longmemeval-official-cot-frozen-default-full-acceptance/v1"
_ATTR_EXPECTED_CASES = 500
_ATTR_DEFAULT_RETRIEVAL_DEPTH = 10
_ATTR_DEFAULT_CONTEXT_BUDGET = 32_768
_ATTR_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ATTR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ATTR_DATE = re.compile(
    r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\D+(\d{1,2}):(\d{2}))?"
)
_ATTR_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_ATTR_PREFERENCE = re.compile(
    r"\b(?:prefer|preference|like|likes|love|enjoy|avoid|dislike|rather|"
    r"experience|experienced|tried|nostalgic|favorite|favourite|constraint)\b",
    re.IGNORECASE,
)
_ATTR_TEMPORAL = re.compile(
    r"\b(?:before|after|earlier|later|first|last|then|when|while|"
    r"yesterday|today|tomorrow|week|month|year|january|february|march|"
    r"april|may|june|july|august|september|october|november|december|"
    r"\d{4}|\d{1,2}[/-]\d{1,2})\b",
    re.IGNORECASE,
)
_ATTR_FORBIDDEN_PUBLIC_KEYS = frozenset(
    {
        "question",
        "answer",
        "prompt",
        "response",
        "body",
        "body_json",
        "content",
        "turns",
        "sessions",
        "secret",
        "credential",
        "password",
        "token",
        "api_key",
        "authorization",
    }
)
_ATTR_FORBIDDEN_ID_MARKERS = frozenset(
    {"secret", "credential", "password", "api_key", "access_token", "authorization"}
)
_ATTR_REASON_CODES = frozenset(
    {
        "no_attributable_failure",
        "required_evidence_absent_from_selected_context",
        "required_evidence_selected_but_poorly_assembled",
        "answer_synthesis_failed",
        "temporal_semantics_mishandled",
        "version_semantics_mishandled",
        "preference_provenance_mishandled",
        "multi_session_composition_unresolved",
        "selected_context_effect",
        "required_evidence_selected",
        "answer_synthesis_candidate",
        "temporal_anchor_observed",
        "latest_version_selected",
        "preference_user_evidence_observed",
        "source_tokens_preserved",
    }
)
_ATTR_CASE_FIELDS = frozenset(
    {
        "question_id",
        "question_type",
        "retrieval_depth",
        "context_budget_tokens",
        "required_evidence_count",
        "selected_required_count",
        "missing_required_count",
        "required_rank_vector",
        "best_required_rank",
        "worst_required_rank",
        "all_required_selected",
        "selected_context_tokens_est",
        "budget_pressure_observed",
        "budget_ok",
        "source_token_preserved",
        "latest_version_selected",
        "latest_version_rank",
        "temporal_anchor_present",
        "user_evidence_present",
        "preference_assistant_only",
        "vault_correct",
        "fullcontext_correct",
        "oracle_correct",
        "stateless_correct",
        "failure_observed",
        "primary_reason",
        "reason_codes",
    }
)
_ATTR_REPORT_FIELDS = frozenset(
    {
        "schema",
        "benchmark",
        "claim_boundary",
        "offline",
        "provider_calls",
        "answerer_calls",
        "judge_calls",
        "raw_inputs_captured",
        "metric_classes",
        "configuration",
        "summary",
        "artifacts",
        "judged_qa_reference",
        "selection_recovery",
        "provider_free_attribution",
        "selected_slice_recovery_reference",
        "fixture_reference",
        "candidate_gate",
        "cases",
        "projection_sha256",
        "signature_sha256",
    }
)


class AttributionError(ValueError):
    """Raised when an attribution input or public report is malformed."""


def stable_json(value: Any) -> str:
    """Return the canonical JSON representation used by all commitments."""
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AttributionError("value is not canonical JSON") from exc


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ATTR_ID.fullmatch(value):
        raise AttributionError(f"{field} must be a bounded identifier")
    lowered = value.lower()
    if any(marker in lowered for marker in _ATTR_FORBIDDEN_ID_MARKERS):
        raise AttributionError(f"{field} contains a forbidden private marker")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ATTR_SHA256.fullmatch(value):
        raise AttributionError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise AttributionError(f"{field} must be boolean")
    return value


def _nonempty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttributionError(f"{field} must be non-empty text")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AttributionError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value == 0:
        raise AttributionError(f"{field} must be positive")
    return value


def _words(value: Any) -> set[str]:
    return {word.lower() for word in _ATTR_WORD.findall(str(value)) if len(word) > 2}


def _date_key(value: Any) -> tuple[int, int, int, int, int]:
    match = _ATTR_DATE.search(str(value or ""))
    if not match:
        return (9_999, 12, 31, 23, 59)
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _token_sequence(value: Any) -> list[str]:
    return [word.lower() for word in _ATTR_WORD.findall(str(value)) if len(word) > 2]


def _is_token_subsequence(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return True
    position = 0
    for token in haystack:
        if token == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


def _turn_text(turn: Mapping[str, Any]) -> str:
    return f"{turn.get('role', '')}: {turn.get('content', '')}"


def _render_full_context(selected: Iterable[Mapping[str, Any]]) -> str:
    blocks: list[str] = []
    for session in selected:
        sid = str(session.get("session_id", "unknown"))
        date = str(session.get("date", ""))
        header = f"[session {sid}" + (f" | {date}" if date else "") + "]"
        turns = session.get("turns", [])
        body = "\n".join(_turn_text(turn) for turn in turns if isinstance(turn, Mapping))
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks) or "(no prior conversation history is available)"


def _session_index(case: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sessions = case.get("sessions", [])
    if not isinstance(sessions, list):
        return {}
    return {
        str(session["session_id"]): session
        for session in sessions
        if isinstance(session, Mapping) and isinstance(session.get("session_id"), str)
    }


def _source_features(
    case: Mapping[str, Any],
    required_ids: list[str],
    ranked_ids: list[str],
    *,
    retrieval_depth: int,
    context_budget_tokens: int,
) -> dict[str, Any]:
    """Compute bounded source/provenance features without returning source text."""
    sessions = _session_index(case)
    ranked_prefix = ranked_ids[:retrieval_depth]
    ranks = {sid: index + 1 for index, sid in enumerate(ranked_prefix)}
    required_ranks = [ranks.get(sid) for sid in required_ids]
    selected_required = [sid for sid in required_ids if sid in ranks]
    selected_sessions = [sessions[sid] for sid in ranked_prefix if sid in sessions]
    rendered = _render_full_context(selected_sessions)
    all_required_selected = len(selected_required) == len(required_ids)

    latest_id: str | None = None
    if str(case.get("question_type", "")) == "knowledge-update":
        dated = [sid for sid in required_ids if sid in sessions and sessions[sid].get("date")]
        if dated:
            latest_id = max(dated, key=lambda sid: _date_key(sessions[sid].get("date")))

    temporal_anchor_present = False
    user_evidence_present = False
    preference_assistant_only = False
    user_relevant = False
    assistant_relevant = False
    query_terms = _words(case.get("question", ""))
    for sid in selected_required:
        session = sessions.get(sid, {})
        turns = session.get("turns", [])
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            content = str(turn.get("content", ""))
            role = str(turn.get("role", "")).lower()
            overlap = bool(query_terms & _words(content))
            preference = bool(_ATTR_PREFERENCE.search(content))
            if role == "user" and (overlap or preference):
                user_relevant = True
            if role != "user" and (overlap or preference):
                assistant_relevant = True
            if _ATTR_TEMPORAL.search(content) or _ATTR_DATE.search(str(session.get("date", ""))):
                temporal_anchor_present = True
    user_evidence_present = user_relevant
    preference_assistant_only = assistant_relevant and not user_relevant

    context_tokens = _estimate_tokens(rendered)
    # The frozen full-context arm does not enforce the ranked-snippet budget;
    # retain pressure as telemetry but do not turn it into an assembly failure.
    budget_pressure_observed = context_tokens > context_budget_tokens
    budget_ok = True
    rendered_tokens = _token_sequence(rendered)
    source_token_preserved = all(
        sid in sessions
        and _is_token_subsequence(
            _token_sequence(
                "\n".join(
                    _turn_text(turn)
                    for turn in sessions[sid].get("turns", [])
                    if isinstance(turn, Mapping)
                )
            ),
            rendered_tokens,
        )
        for sid in selected_required
    ) and all_required_selected
    projection = case.get("projection")
    if isinstance(projection, Mapping):
        if "source_token_preserved" in projection:
            source_token_preserved = _bool(
                projection["source_token_preserved"], "projection.source_token_preserved"
            )
        if "budget_ok" in projection:
            budget_ok = _bool(projection["budget_ok"], "projection.budget_ok")

    return {
        "required_ranks": required_ranks,
        "selected_required_count": len(selected_required),
        "missing_required_count": len(required_ids) - len(selected_required),
        "all_required_selected": all_required_selected,
        "selected_context_tokens_est": context_tokens,
        "budget_pressure_observed": budget_pressure_observed,
        "budget_ok": budget_ok,
        "source_token_preserved": source_token_preserved,
        "latest_version_rank": ranks.get(latest_id) if latest_id else None,
        "latest_version_selected": bool(latest_id and latest_id in ranks),
        "temporal_anchor_present": temporal_anchor_present,
        "user_evidence_present": user_evidence_present,
        "preference_assistant_only": preference_assistant_only,
    }


def _classify_case(case: Mapping[str, Any], features: Mapping[str, Any]) -> tuple[str, list[str]]:
    outcomes = case.get("outcomes", {})
    vault_correct = _bool(outcomes.get("vault_correct"), "outcomes.vault_correct")
    fullcontext_correct = _bool(outcomes.get("fullcontext_correct"), "outcomes.fullcontext_correct")
    oracle_correct = _bool(outcomes.get("oracle_correct"), "outcomes.oracle_correct")
    qtype = str(case.get("question_type", "unknown"))
    failure = not vault_correct and oracle_correct
    if not failure:
        return "no_attributable_failure", ["no_attributable_failure"]

    reasons: list[str] = []
    if features["missing_required_count"]:
        primary = "required_evidence_absent_from_selected_context"
        reasons.append(primary)
    elif not features["source_token_preserved"] or not features["budget_ok"]:
        primary = "required_evidence_selected_but_poorly_assembled"
        reasons.append(primary)
    elif qtype == "single-session-preference" and features["user_evidence_present"]:
        primary = "preference_provenance_mishandled"
        reasons.append(primary)
    elif qtype == "knowledge-update" and features["latest_version_selected"]:
        primary = "version_semantics_mishandled"
        reasons.append(primary)
    elif qtype == "temporal-reasoning" and features["temporal_anchor_present"]:
        primary = "temporal_semantics_mishandled"
        reasons.append(primary)
    elif qtype == "multi-session":
        primary = "multi_session_composition_unresolved"
        reasons.append(primary)
    elif fullcontext_correct:
        primary = "selected_context_effect"
        reasons.append(primary)
    else:
        primary = "answer_synthesis_failed"
        reasons.append(primary)

    if features["all_required_selected"]:
        reasons.append("required_evidence_selected")
    if features["source_token_preserved"]:
        reasons.append("source_tokens_preserved")
    if fullcontext_correct and features["all_required_selected"]:
        reasons.append("selected_context_effect")
    if not fullcontext_correct and features["all_required_selected"]:
        reasons.append("answer_synthesis_candidate")
    if qtype == "single-session-preference" and features["user_evidence_present"]:
        reasons.append("preference_user_evidence_observed")
    if qtype == "knowledge-update" and features["latest_version_selected"]:
        reasons.append("latest_version_selected")
    if qtype == "temporal-reasoning" and features["temporal_anchor_present"]:
        reasons.append("temporal_anchor_observed")
    return primary, reasons


def _case_from_input(
    case: Mapping[str, Any],
    *,
    retrieval_depth: int,
    context_budget_tokens: int,
) -> dict[str, Any]:
    question_id = _id(case.get("question_id"), "case.question_id")
    question_type = _id(case.get("question_type"), "case.question_type")
    required = case.get("required_evidence_ids")
    ranked = case.get("ranked_ids")
    if not isinstance(required, list) or not required:
        raise AttributionError("case.required_evidence_ids must be a non-empty list")
    if not isinstance(ranked, list):
        raise AttributionError("case.ranked_ids must be a list")
    required_ids = [_id(value, "required_evidence_id") for value in required]
    ranked_ids = [_id(value, "ranked_id") for value in ranked]
    if len(required_ids) != len(set(required_ids)):
        raise AttributionError("required evidence IDs must be unique")
    if len(ranked_ids) != len(set(ranked_ids)):
        raise AttributionError("ranked IDs must be unique")
    features = _source_features(
        case,
        required_ids,
        ranked_ids,
        retrieval_depth=retrieval_depth,
        context_budget_tokens=context_budget_tokens,
    )
    outcomes = case.get("outcomes")
    if not isinstance(outcomes, Mapping):
        raise AttributionError("case.outcomes must be an object")
    values = {
        key: _bool(outcomes.get(key), f"outcomes.{key}")
        for key in ("vault_correct", "fullcontext_correct", "oracle_correct", "stateless_correct")
    }
    primary, reasons = _classify_case(case, features)
    return {
        "question_id": question_id,
        "question_type": question_type,
        "retrieval_depth": retrieval_depth,
        "context_budget_tokens": context_budget_tokens,
        "required_evidence_count": len(required_ids),
        "selected_required_count": features["selected_required_count"],
        "missing_required_count": features["missing_required_count"],
        "required_rank_vector": features["required_ranks"],
        "best_required_rank": min(
            (rank for rank in features["required_ranks"] if rank is not None),
            default=None,
        ),
        "worst_required_rank": max(
            (rank for rank in features["required_ranks"] if rank is not None),
            default=None,
        ),
        "all_required_selected": features["all_required_selected"],
        "selected_context_tokens_est": features["selected_context_tokens_est"],
        "budget_pressure_observed": features["budget_pressure_observed"],
        "budget_ok": features["budget_ok"],
        "source_token_preserved": features["source_token_preserved"],
        "latest_version_selected": features["latest_version_selected"],
        "latest_version_rank": features["latest_version_rank"],
        "temporal_anchor_present": features["temporal_anchor_present"],
        "user_evidence_present": features["user_evidence_present"],
        "preference_assistant_only": features["preference_assistant_only"],
        **values,
        "failure_observed": not values["vault_correct"] and values["oracle_correct"],
        "primary_reason": primary,
        "reason_codes": reasons,
    }


def validate_fixture(cases: Any) -> None:
    if not isinstance(cases, list) or not cases:
        raise AttributionError("fixture cases must be a non-empty list")
    ids: list[str] = []
    allowed = {
        "scenario",
        "question_id",
        "question_type",
        "question",
        "question_date",
        "required_evidence_ids",
        "ranked_ids",
        "sessions",
        "outcomes",
        "projection",
    }
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise AttributionError(f"fixture case {index} must be an object")
        unknown = set(case) - allowed
        if unknown:
            raise AttributionError(f"fixture case contains unknown field: {sorted(unknown)[0]}")
        ids.append(_id(case.get("question_id"), f"fixture case {index}.question_id"))
        _id(case.get("scenario"), f"fixture case {index}.scenario")
        _id(case.get("question_type"), f"fixture case {index}.question_type")
        for field in ("question", "question_date"):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise AttributionError(f"fixture case {index}.{field} must be non-empty text")
        required = case.get("required_evidence_ids")
        ranked = case.get("ranked_ids")
        if not isinstance(required, list) or not required:
            raise AttributionError(f"fixture case {index}.required_evidence_ids is invalid")
        if not isinstance(ranked, list):
            raise AttributionError(f"fixture case {index}.ranked_ids is invalid")
        required_ids = [_id(value, "fixture required evidence") for value in required]
        ranked_ids = [_id(value, "fixture ranked id") for value in ranked]
        if len(required_ids) != len(set(required_ids)):
            raise AttributionError("fixture required evidence IDs must be unique")
        if len(ranked_ids) != len(set(ranked_ids)):
            raise AttributionError("fixture ranked IDs must be unique")
        sessions = case.get("sessions")
        if not isinstance(sessions, list):
            raise AttributionError(f"fixture case {index}.sessions is invalid")
        session_ids: list[str] = []
        for session in sessions:
            if not isinstance(session, Mapping):
                raise AttributionError("fixture sessions must contain objects")
            if set(session) != {"session_id", "date", "turns"}:
                raise AttributionError("fixture session fields are not exact")
            sid = _id(session.get("session_id"), "fixture session_id")
            session_ids.append(sid)
            if not isinstance(session.get("date"), str):
                raise AttributionError("fixture session date must be text")
            turns = session.get("turns")
            if not isinstance(turns, list) or not turns:
                raise AttributionError("fixture session turns must be non-empty")
            for turn in turns:
                if not isinstance(turn, Mapping) or set(turn) != {"role", "content"}:
                    raise AttributionError("fixture turns must have role/content")
                if not isinstance(turn["role"], str) or not isinstance(turn["content"], str):
                    raise AttributionError("fixture turn fields must be text")
        if len(session_ids) != len(set(session_ids)):
            raise AttributionError("fixture session IDs must be unique")
        if not set(required_ids) <= set(session_ids):
            raise AttributionError("fixture required IDs must name fixture sessions")
        outcomes = case.get("outcomes")
        if not isinstance(outcomes, Mapping) or set(outcomes) != {
            "vault_correct", "fullcontext_correct", "oracle_correct", "stateless_correct"
        }:
            raise AttributionError("fixture outcomes are not exact")
        for key, value in outcomes.items():
            _bool(value, f"fixture outcomes.{key}")
        projection = case.get("projection")
        if projection is not None:
            if not isinstance(projection, Mapping) or set(projection) != {"source_token_preserved", "budget_ok"}:
                raise AttributionError("fixture projection is not exact")
            _bool(projection["source_token_preserved"], "fixture projection.source_token_preserved")
            _bool(projection["budget_ok"], "fixture projection.budget_ok")
    if len(ids) != len(set(ids)):
        raise AttributionError("fixture question IDs must be unique")


def load_synthetic_fixture(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttributionError(f"cannot load synthetic fixture: {path.name}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema") != _ATTR_FIXTURE_SCHEMA:
        raise AttributionError("unsupported synthetic fixture schema")
    if payload.get("synthetic_only") is not True:
        raise AttributionError("fixture must declare synthetic_only=true")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise AttributionError("fixture cases must be a list")
    validate_fixture(cases)
    return copy.deepcopy(cases)


def _sealed_report(base: dict[str, Any]) -> dict[str, Any]:
    projection = sha256_json(base)
    with_projection = {**base, "projection_sha256": projection}
    signature = sha256_json(with_projection)
    return {**with_projection, "signature_sha256": signature}


def _summary(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    cases = list(cases)
    by_reason = Counter(str(case["primary_reason"]) for case in cases)
    reason_codes = Counter(
        reason
        for case in cases
        for reason in case.get("reason_codes", [])
    )
    by_type: dict[str, dict[str, Any]] = {}
    for qtype in sorted({str(case["question_type"]) for case in cases}):
        rows = [case for case in cases if case["question_type"] == qtype]
        failures = [case for case in rows if case["failure_observed"]]
        by_type[qtype] = {
            "n": len(rows),
            "vault_correct": sum(bool(row["vault_correct"]) for row in rows),
            "fullcontext_correct": sum(bool(row["fullcontext_correct"]) for row in rows),
            "oracle_correct": sum(bool(row["oracle_correct"]) for row in rows),
            "oracle_right_vault_wrong": len(failures),
            "all_required_selected": sum(bool(row["all_required_selected"]) for row in rows),
            "selected_context_budget_ok": sum(bool(row["budget_ok"]) for row in rows),
            "budget_pressure_observed": sum(bool(row["budget_pressure_observed"]) for row in rows),
            "source_token_preserved": sum(bool(row["source_token_preserved"]) for row in rows),
            "reason_code_counts": dict(sorted(
                Counter(
                    reason
                    for row in rows
                    for reason in row.get("reason_codes", [])
                ).items()
            )),
            "failure_reason_counts": dict(sorted(Counter(row["primary_reason"] for row in failures).items())),
        }
    return {
        "n": len(cases),
        "attributable_failure_count": sum(bool(case["failure_observed"]) for case in cases),
        "primary_reason_counts": dict(sorted(by_reason.items())),
        "reason_code_counts": dict(sorted(reason_codes.items())),
        "by_question_type": by_type,
    }


def _fixture_candidate_gate(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    cases = list(cases)
    reasons = Counter(case["primary_reason"] for case in cases)
    checks = {
        "required_scenarios_present": len(cases) >= 4,
        "bounded_case_projection": all(set(case) == _ATTR_CASE_FIELDS for case in cases),
        "fixture_reason_classes_observed": all(
            reason in reasons
            for reason in (
                "required_evidence_absent_from_selected_context",
                "required_evidence_selected_but_poorly_assembled",
                "answer_synthesis_failed",
            )
        ),
    }
    return {
        "provider_free_gate_passed": all(checks.values()),
        "specific_testable_candidate": "source_preserving_role_and_date_anchored_context_projection",
        "hypothesis": (
            "When required evidence is already selected, a role-labeled, date-anchored, "
            "source-preserving context projection will reduce answer-facing failures without "
            "changing retrieval membership."
        ),
        "falsification": (
            "A same-denominator paired evaluation shows no gain on evidence-present failures, "
            "or any regression on the all-correct stratum or source-token preservation."
        ),
        "provider_free_regression_test": "benchmark.longmemeval.test_failure_attribution",
        "synthetic_checks": checks,
        "paid_canary_authorized": False,
        "paid_canary_started": False,
        "paid_canary_next_step": "separate_paired_authorization_required",
    }


def build_fixture_report(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a small synthetic reference report for the committed fixtures."""
    cases = list(cases)
    validate_fixture(cases)
    projected = [
        _case_from_input(
            case,
            retrieval_depth=_ATTR_DEFAULT_RETRIEVAL_DEPTH,
            context_budget_tokens=_ATTR_DEFAULT_CONTEXT_BUDGET,
        )
        for case in cases
    ]
    projected.sort(key=lambda row: row["question_id"])
    base = {
        "schema": _ATTR_SCHEMA,
        "benchmark": "perseus-vault-longmemeval-failure-attribution",
        "claim_boundary": "synthetic reference only; not judged QA accuracy",
        "offline": True,
        "provider_calls": 0,
        "answerer_calls": 0,
        "judge_calls": 0,
        "raw_inputs_captured": False,
        "metric_classes": ["provider_free_attribution", "synthetic_fixture_reference"],
        "configuration": {
            "retrieval_depth": _ATTR_DEFAULT_RETRIEVAL_DEPTH,
            "context_mode": "full",
            "context_budget_tokens": _ATTR_DEFAULT_CONTEXT_BUDGET,
        },
        "summary": _summary(projected),
        "provider_free_attribution": {
            "metric_class": "provider_free_attribution",
            "summary": _summary(projected),
            "reason_code_vocabulary": sorted(_ATTR_REASON_CODES),
        },
        "cases": projected,
        "candidate_gate": _fixture_candidate_gate(projected),
        "artifacts": [],
    }
    return _sealed_report(base)


def _validate_accepted_baseline(
    baseline: Mapping[str, Any], *, expected_cases: int
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    if baseline.get("schema") != _ATTR_BASELINE_SCHEMA:
        raise AttributionError("unsupported accepted baseline schema")
    if baseline.get("status") != "accepted_with_correction":
        raise AttributionError("baseline is not the accepted corrected report")
    if baseline.get("raw_payloads_excluded") is not True:
        raise AttributionError("baseline must declare raw_payloads_excluded=true")
    if baseline.get("answer_prompt") != "official-cot":
        raise AttributionError("baseline answer prompt is not official-cot")
    if baseline.get("hypothesis_mode") != "complete-response":
        raise AttributionError("baseline does not retain complete responses")
    if baseline.get("retrieval", {}).get("mode") != "hybrid":
        raise AttributionError("baseline retrieval mode is not hybrid")
    if baseline.get("retrieval", {}).get("k") != _ATTR_DEFAULT_RETRIEVAL_DEPTH:
        raise AttributionError("baseline retrieval depth is not the frozen depth")
    denominator = baseline.get("denominator")
    if not isinstance(denominator, Mapping) or any(
        denominator.get(field) != expected_cases * 4
        for field in ("planned_cells", "attempted_cells", "graded_cells")
    ):
        raise AttributionError("baseline denominator is not the four-arm 500-case contract")
    if denominator.get("answer_errors") != 0 or denominator.get("judge_errors") != 0:
        raise AttributionError("baseline contains answer or judge errors")
    systems = baseline.get("systems")
    expected_systems = {"stateless", "fullcontext", "perseus-vault", "oracle"}
    if not isinstance(systems, Mapping) or set(systems) != expected_systems:
        raise AttributionError("baseline systems are not the frozen four-arm set")
    for system in expected_systems:
        summary = systems[system]
        if not isinstance(summary, Mapping):
            raise AttributionError(f"baseline system {system} is malformed")
        if summary.get("n_attempted") != expected_cases or summary.get("n_graded") != expected_cases:
            raise AttributionError(f"baseline system {system} denominator mismatch")
        if summary.get("answer_errors") != 0 or summary.get("judge_errors") != 0:
            raise AttributionError(f"baseline system {system} contains errors")
    rows = baseline.get("per_question")
    if not isinstance(rows, list) or len(rows) != expected_cases * 4:
        raise AttributionError("baseline per-question rows do not contain exactly four arms per case")
    by_id: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise AttributionError(f"baseline row {index} is not an object")
        qid = _id(row.get("question_id"), f"baseline row {index}.question_id")
        system = row.get("system")
        if system not in expected_systems:
            raise AttributionError(f"baseline row {index} has unknown system")
        if not isinstance(row.get("question_type"), str):
            raise AttributionError(f"baseline row {index} is missing question_type")
        _bool(row.get("correct"), f"baseline row {index}.correct")
        if system in by_id[qid]:
            raise AttributionError(f"baseline duplicate row for {qid}/{system}")
        by_id[qid][system] = {
            "question_type": row["question_type"],
            "correct": row["correct"],
        }
    if len(by_id) != expected_cases or any(set(rows_for_id) != expected_systems for rows_for_id in by_id.values()):
        raise AttributionError("baseline must contain exactly four unique systems for every case")
    return dict(by_id), {
        "answerer_model": baseline.get("answerer_model"),
        "judge_model": baseline.get("judge_model"),
        "systems": {
            system: {
                "n": systems[system].get("n_graded"),
                "correct": systems[system].get("correct"),
                "accuracy": systems[system].get("accuracy"),
            }
            for system in sorted(expected_systems)
        },
    }


def _validate_retrieval_replay(
    replay: Mapping[str, Any], *, expected_cases: int, expected_depth: int
) -> dict[str, dict[str, Any]]:
    if replay.get("offline") is not True:
        raise AttributionError("retrieval replay must declare offline=true")
    if replay.get("n_instances") != expected_cases:
        raise AttributionError("retrieval replay case count mismatch")
    rows = replay.get("per_question")
    if not isinstance(rows, list) or len(rows) != expected_cases:
        raise AttributionError("retrieval replay must contain one row per case")
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise AttributionError(f"retrieval row {index} is not an object")
        qid = _id(row.get("question_id"), f"retrieval row {index}.question_id")
        qtype = _id(row.get("question_type"), f"retrieval row {index}.question_type")
        evidence = row.get("evidence")
        modes = row.get("modes")
        if not isinstance(evidence, list) or not evidence:
            raise AttributionError(f"retrieval row {index} has no evidence set")
        if not isinstance(modes, Mapping) or not isinstance(modes.get("hybrid"), Mapping):
            raise AttributionError(f"retrieval row {index} has no hybrid projection")
        top = modes["hybrid"].get("top")
        if not isinstance(top, list) or len(top) < expected_depth:
            raise AttributionError(f"retrieval row {index} has no complete hybrid prefix")
        evidence_ids = [_id(value, f"retrieval row {index}.evidence") for value in evidence]
        top_ids = [_id(value, f"retrieval row {index}.hybrid.top") for value in top]
        if len(evidence_ids) != len(set(evidence_ids)) or len(top_ids) != len(set(top_ids)):
            raise AttributionError(f"retrieval row {index} contains duplicate IDs")
        if qid in result:
            raise AttributionError(f"retrieval duplicate question ID: {qid}")
        result[qid] = {"question_type": qtype, "evidence": evidence_ids, "top": top_ids}
    if len(result) != expected_cases:
        raise AttributionError("retrieval replay question IDs are not unique")
    return result


def _dataset_cases(
    dataset: list[Mapping[str, Any]],
    replay: Mapping[str, dict[str, Any]],
    baseline: Mapping[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(dataset, list) or len(dataset) != len(baseline):
        raise AttributionError("dataset must contain exactly the accepted case count")
    by_id: dict[str, dict[str, Any]] = {}
    for index, instance in enumerate(dataset):
        if not isinstance(instance, Mapping):
            raise AttributionError(f"dataset row {index} is not an object")
        qid = _id(instance.get("question_id"), f"dataset row {index}.question_id")
        if qid in by_id or qid not in replay or qid not in baseline:
            raise AttributionError(f"dataset question ID is not aligned: {qid}")
        ids = instance.get("haystack_session_ids")
        dates = instance.get("haystack_dates")
        sessions = instance.get("haystack_sessions")
        if not isinstance(ids, list) or not isinstance(dates, list) or not isinstance(sessions, list):
            raise AttributionError(f"dataset row {index} haystack arrays are malformed")
        if not (len(ids) == len(dates) == len(sessions)):
            raise AttributionError(f"dataset row {index} haystack arrays are not aligned")
        source_sessions = []
        for sid, date, turns in zip(ids, dates, sessions):
            sid = _id(sid, "dataset session ID")
            if not isinstance(date, str) or not isinstance(turns, list):
                raise AttributionError("dataset session date/turns are malformed")
            source_sessions.append({"session_id": sid, "date": date, "turns": turns})
        evidence = replay[qid]["evidence"]
        dataset_gold = instance.get("answer_session_ids")
        if not isinstance(dataset_gold, list) or set(dataset_gold) != set(evidence):
            raise AttributionError(f"dataset/replay required evidence mismatch for {qid}")
        if replay[qid]["question_type"] != instance.get("question_type"):
            raise AttributionError(f"dataset/replay question type mismatch for {qid}")
        by_id[qid] = {
            "scenario": "accepted_500_case",
            "question_id": qid,
            "question_type": instance["question_type"],
            "question": instance.get("question", ""),
            "question_date": instance.get("question_date", ""),
            "required_evidence_ids": evidence,
            "ranked_ids": replay[qid]["top"],
            "sessions": source_sessions,
            "outcomes": {
                "vault_correct": baseline[qid]["perseus-vault"]["correct"],
                "fullcontext_correct": baseline[qid]["fullcontext"]["correct"],
                "oracle_correct": baseline[qid]["oracle"]["correct"],
                "stateless_correct": baseline[qid]["stateless"]["correct"],
            },
        }
    if set(by_id) != set(baseline):
        raise AttributionError("dataset does not cover the accepted baseline")
    return by_id


def _artifact(name: Any, digest: Any, *, n: Any = None) -> dict[str, Any]:
    record: dict[str, Any] = {"name": _id(name, "artifact.name"), "sha256": _sha(digest, "artifact.sha256")}
    if n is not None:
        record["n"] = _nonnegative_int(n, "artifact.n")
    return record


def _reference_selection(cases: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    cases = list(cases)
    by_type: dict[str, dict[str, Any]] = {}
    for qtype in sorted({str(case["question_type"]) for case in cases}):
        rows = [case for case in cases if case["question_type"] == qtype]
        n = len(rows)
        selected = sum(bool(row["all_required_selected"]) for row in rows)
        by_type[qtype] = {
            "n": n,
            "all_required_selected": selected,
            "all_required_selected_rate": round(selected / n, 4) if n else None,
            "mean_missing_required_count": round(
                sum(int(row["missing_required_count"]) for row in rows) / n, 4
            ) if n else None,
        }
    n = len(cases)
    selected = sum(bool(row["all_required_selected"]) for row in cases)
    return {
        "metric_class": "provider_free_selection_recovery",
        "retrieval_depth": _ATTR_DEFAULT_RETRIEVAL_DEPTH,
        "n": n,
        "all_required_selected": selected,
        "all_required_selected_rate": round(selected / n, 4) if n else None,
        "by_question_type": by_type,
    }


def _candidate_gate(
    cases: Iterable[Mapping[str, Any]], fixture_reference: Mapping[str, Any] | None
) -> dict[str, Any]:
    cases = list(cases)
    failures = [case for case in cases if case["failure_observed"]]
    evidence_present = [case for case in failures if case["all_required_selected"]]
    semantic_flags = [
        case for case in evidence_present
        if case["question_type"] in {
            "single-session-preference",
            "knowledge-update",
            "temporal-reasoning",
            "multi-session",
        }
    ]
    fixture_passed = bool(
        fixture_reference
        and (
            fixture_reference.get("provider_free_gate_passed")
            or fixture_reference.get("candidate_gate", {}).get("provider_free_gate_passed")
        )
    )
    candidate_identified = bool(evidence_present and semantic_flags and fixture_passed)
    checks = {
        "same_denominator_available": len(cases) == _ATTR_EXPECTED_CASES,
        "evidence_present_failures_observed": bool(evidence_present),
        "semantic_categories_observed": bool(semantic_flags),
        "synthetic_fixture_gate_passed": fixture_passed,
    }
    return {
        "provider_free_gate_passed": all(checks.values()),
        "candidate_identified": candidate_identified,
        "specific_testable_candidate": (
            "source_preserving_role_and_date_anchored_context_projection"
            if candidate_identified else None
        ),
        "hypothesis": (
            "When required evidence is selected, a role-labeled, date-anchored, source-preserving "
            "context projection can reduce answer-facing failures without changing retrieval membership."
        ) if candidate_identified else None,
        "falsification": (
            "A same-denominator paired paid canary shows no gain on evidence-present failures, "
            "or any regression on the accepted all-correct control stratum or source-token preservation."
        ) if candidate_identified else None,
        "provider_free_regression_test": "benchmark.longmemeval.test_failure_attribution",
        "evidence_present_failure_count": len(evidence_present),
        "semantic_failure_count": len(semantic_flags),
        "checks": checks,
        "paid_canary_authorized": False,
        "paid_canary_started": False,
        "paid_canary_next_step": "separate_paired_authorization_required",
    }


def build_attribution_report(
    baseline: Mapping[str, Any],
    retrieval_replay: Mapping[str, Any],
    dataset: list[Mapping[str, Any]],
    *,
    source_artifacts: Iterable[Mapping[str, Any]] = (),
    fixture_reference: Mapping[str, Any] | None = None,
    selected_slice_reference: Mapping[str, Any] | None = None,
    expected_cases: int = _ATTR_EXPECTED_CASES,
    retrieval_depth: int = _ATTR_DEFAULT_RETRIEVAL_DEPTH,
    context_budget_tokens: int = _ATTR_DEFAULT_CONTEXT_BUDGET,
) -> dict[str, Any]:
    """Build the 500-case bounded attribution report."""
    _positive_int(expected_cases, "expected_cases")
    _positive_int(retrieval_depth, "retrieval_depth")
    _positive_int(context_budget_tokens, "context_budget_tokens")
    baseline_rows, baseline_reference = _validate_accepted_baseline(
        baseline, expected_cases=expected_cases
    )
    replay_rows = _validate_retrieval_replay(
        retrieval_replay,
        expected_cases=expected_cases,
        expected_depth=retrieval_depth,
    )
    if set(baseline_rows) != set(replay_rows):
        raise AttributionError("accepted baseline and retrieval replay IDs do not match")
    for qid in baseline_rows:
        if baseline_rows[qid]["perseus-vault"]["question_type"] != replay_rows[qid]["question_type"]:
            raise AttributionError(f"baseline/replay question type mismatch for {qid}")
    source_cases = _dataset_cases(dataset, replay_rows, baseline_rows)
    projected = [
        _case_from_input(
            source_cases[qid],
            retrieval_depth=retrieval_depth,
            context_budget_tokens=context_budget_tokens,
        )
        for qid in sorted(source_cases)
    ]
    artifacts = [dict(item) for item in source_artifacts]
    for item in artifacts:
        if set(item) - {"name", "sha256", "n"}:
            raise AttributionError("source artifact has an unknown public field")
        _artifact(item["name"], item["sha256"], n=item.get("n"))
    base: dict[str, Any] = {
        "schema": _ATTR_SCHEMA,
        "benchmark": "perseus-vault-longmemeval-failure-attribution",
        "claim_boundary": (
            "Provider-free attribution over the accepted 500-case frozen-default denominator. "
            "Attribution flags and proxies are not new judged QA accuracy and do not authorize paid work."
        ),
        "offline": True,
        "provider_calls": 0,
        "answerer_calls": 0,
        "judge_calls": 0,
        "raw_inputs_captured": False,
        "metric_classes": [
            "judged_qa_reference",
            "provider_free_selection_recovery",
            "provider_free_attribution",
            "selected_slice_recovery_reference",
        ],
        "configuration": {
            "denominator_cases": expected_cases,
            "retrieval_depth": retrieval_depth,
            "context_mode": "full",
            "context_budget_tokens": context_budget_tokens,
            "official_cot_preserved": True,
            "default_behavior_changed": False,
        },
        "summary": _summary(projected),
        "artifacts": artifacts,
        "judged_qa_reference": {
            "metric_class": "judged_qa_reference",
            "source_status": "accepted_with_correction",
            "answerer_model": baseline_reference["answerer_model"],
            "judge_model": baseline_reference["judge_model"],
            "systems": baseline_reference["systems"],
            "claim": "historical accepted baseline only; no new judge call was made",
        },
        "selection_recovery": _reference_selection(projected),
        "provider_free_attribution": {
            "metric_class": "provider_free_attribution",
            "summary": _summary(projected),
            "reason_code_vocabulary": sorted(_ATTR_REASON_CODES),
        },
        "selected_slice_recovery_reference": selected_slice_reference or {
            "available": False,
            "metric_class": "selected_slice_recovery",
        },
        "fixture_reference": fixture_reference or {"available": False},
        "candidate_gate": _candidate_gate(projected, fixture_reference),
        "cases": projected,
    }
    return _sealed_report(base)


def _scan_public_keys(value: Any, *, path: str = "report") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in _ATTR_FORBIDDEN_PUBLIC_KEYS:
                raise AttributionError(f"forbidden raw field in public report: {path}.{key}")
            _scan_public_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _scan_public_keys(nested, path=f"{path}[{index}]")


def validate_report(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping):
        raise AttributionError("report must be an object")
    required = {
        "schema", "benchmark", "claim_boundary", "offline", "provider_calls", "answerer_calls",
        "judge_calls", "raw_inputs_captured", "metric_classes", "configuration", "summary",
        "provider_free_attribution", "candidate_gate", "cases", "artifacts", "projection_sha256",
        "signature_sha256",
    }
    unknown = set(report) - _ATTR_REPORT_FIELDS
    if unknown:
        raise AttributionError(f"report contains unknown field: {sorted(unknown)[0]}")
    missing = required - set(report)
    if missing:
        raise AttributionError(f"report missing field: {sorted(missing)[0]}")
    if report.get("schema") != _ATTR_SCHEMA:
        raise AttributionError("unsupported attribution report schema")
    if report.get("benchmark") != "perseus-vault-longmemeval-failure-attribution":
        raise AttributionError("report benchmark name is invalid")
    if report.get("offline") is not True or report.get("provider_calls") != 0:
        raise AttributionError("report is not provider-free")
    if report.get("answerer_calls") != 0 or report.get("judge_calls") != 0:
        raise AttributionError("report contains answerer/judge calls")
    if report.get("raw_inputs_captured") is not False:
        raise AttributionError("report must declare raw_inputs_captured=false")
    metric_classes = report.get("metric_classes")
    if not isinstance(metric_classes, list) or any(
        not isinstance(value, str) or not value.strip() for value in metric_classes
    ):
        raise AttributionError("metric_classes must be a list of non-empty text")
    if len(metric_classes) != len(set(metric_classes)):
        raise AttributionError("metric_classes must be duplicate-free")
    _nonempty_text(report.get("claim_boundary"), "claim_boundary")
    configuration = report.get("configuration")
    if not isinstance(configuration, Mapping):
        raise AttributionError("configuration is malformed")
    config_allowed = {
        "denominator_cases", "retrieval_depth", "context_mode", "context_budget_tokens",
        "official_cot_preserved", "default_behavior_changed",
    }
    if set(configuration) - config_allowed:
        raise AttributionError("configuration contains an unknown field")
    if "denominator_cases" in configuration:
        if set(configuration) != config_allowed:
            raise AttributionError("full report configuration is incomplete")
        _positive_int(configuration.get("denominator_cases"), "configuration.denominator_cases")
        _bool(configuration.get("official_cot_preserved"), "configuration.official_cot_preserved")
        _bool(configuration.get("default_behavior_changed"), "configuration.default_behavior_changed")
    elif set(configuration) != {"retrieval_depth", "context_mode", "context_budget_tokens"}:
        raise AttributionError("fixture report configuration is not exact")
    _positive_int(configuration.get("retrieval_depth"), "configuration.retrieval_depth")
    _id(configuration.get("context_mode"), "configuration.context_mode")
    _positive_int(configuration.get("context_budget_tokens"), "configuration.context_budget_tokens")

    artifacts = report.get("artifacts")
    if not isinstance(artifacts, list):
        raise AttributionError("artifacts must be a list")
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise AttributionError(f"artifact {index} must be an object")
        if set(artifact) - {"name", "sha256", "n"}:
            raise AttributionError(f"artifact {index} contains an unknown field")
        _artifact(artifact.get("name"), artifact.get("sha256"), n=artifact.get("n"))
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases:
        raise AttributionError("report cases must be non-empty")
    if any(not isinstance(case, Mapping) for case in cases):
        raise AttributionError("report cases must be objects")
    qids = [case.get("question_id") for case in cases]
    if any(not isinstance(qid, str) for qid in qids):
        raise AttributionError("report question IDs must be text")
    if len(qids) != len(set(qids)):
        raise AttributionError("report question IDs must be unique")
    if qids != sorted(qids):
        raise AttributionError("report cases must be sorted by question ID")
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping) or set(case) != _ATTR_CASE_FIELDS:
            raise AttributionError(f"case {index} does not match the bounded public schema")
        _id(case.get("question_id"), f"case {index}.question_id")
        _id(case.get("question_type"), f"case {index}.question_type")
        _positive_int(case.get("retrieval_depth"), f"case {index}.retrieval_depth")
        _positive_int(case.get("context_budget_tokens"), f"case {index}.context_budget_tokens")
        for field in (
            "required_evidence_count", "selected_required_count", "missing_required_count",
            "selected_context_tokens_est",
        ):
            _nonnegative_int(case.get(field), f"case {index}.{field}")
        ranks = case.get("required_rank_vector")
        if not isinstance(ranks, list) or any(
            rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0)
            for rank in ranks
        ):
            raise AttributionError(f"case {index}.required_rank_vector is invalid")
        for field in (
            "all_required_selected", "budget_pressure_observed", "budget_ok", "source_token_preserved", "latest_version_selected",
            "temporal_anchor_present", "user_evidence_present", "preference_assistant_only",
            "vault_correct", "fullcontext_correct", "oracle_correct", "stateless_correct", "failure_observed",
        ):
            _bool(case.get(field), f"case {index}.{field}")
        for field in ("best_required_rank", "worst_required_rank", "latest_version_rank"):
            value = case.get(field)
            if value is not None:
                _positive_int(value, f"case {index}.{field}")
        primary = case.get("primary_reason")
        if not isinstance(primary, str) or primary not in _ATTR_REASON_CODES:
            raise AttributionError(f"case {index} has unknown primary reason")
        reason_codes = case.get("reason_codes")
        if not isinstance(reason_codes, list) or any(
            not isinstance(reason, str) for reason in reason_codes
        ) or len(reason_codes) != len(set(reason_codes)):
            raise AttributionError(f"case {index}.reason_codes is invalid")
        if any(reason not in _ATTR_REASON_CODES for reason in reason_codes):
            raise AttributionError(f"case {index} has unknown reason code")
    expected_summary = _summary(cases)
    if report.get("summary") != expected_summary:
        raise AttributionError("summary does not match the projected cases")
    provider_projection = report.get("provider_free_attribution")
    if not isinstance(provider_projection, Mapping):
        raise AttributionError("provider_free_attribution is malformed")
    if provider_projection.get("metric_class") != "provider_free_attribution":
        raise AttributionError("provider_free_attribution metric class is invalid")
    if provider_projection.get("summary") != expected_summary:
        raise AttributionError("provider_free_attribution summary does not match cases")
    if provider_projection.get("reason_code_vocabulary") != sorted(_ATTR_REASON_CODES):
        raise AttributionError("provider_free_attribution vocabulary is invalid")
    if set(provider_projection) != {"metric_class", "summary", "reason_code_vocabulary"}:
        raise AttributionError("provider_free_attribution contains an unknown field")
    for index, case in enumerate(cases):
        required_count = case["required_evidence_count"]
        ranks = case["required_rank_vector"]
        if len(ranks) != required_count:
            raise AttributionError(f"case {index} rank vector length mismatch")
        if case["selected_required_count"] + case["missing_required_count"] != required_count:
            raise AttributionError(f"case {index} evidence count mismatch")
        if case["all_required_selected"] != (case["missing_required_count"] == 0):
            raise AttributionError(f"case {index} selection flag mismatch")
        expected_failure = not case["vault_correct"] and case["oracle_correct"]
        if case["failure_observed"] != expected_failure:
            raise AttributionError(f"case {index} failure flag mismatch")
        present_ranks = [rank for rank in ranks if rank is not None]
        expected_best = min(present_ranks) if present_ranks else None
        expected_worst = max(present_ranks) if present_ranks else None
        if case["best_required_rank"] != expected_best or case["worst_required_rank"] != expected_worst:
            raise AttributionError(f"case {index} rank extrema mismatch")
        if case["primary_reason"] not in case["reason_codes"]:
            raise AttributionError(f"case {index} primary reason is not declared")
        if not case["failure_observed"] and case["primary_reason"] != "no_attributable_failure":
            raise AttributionError(f"case {index} non-failure has an attribution reason")
    if "fixture_reference" in report:
        fixture_reference = report["fixture_reference"]
        if not isinstance(fixture_reference, Mapping):
            raise AttributionError("fixture_reference is malformed")
        if fixture_reference.get("available") is False:
            if set(fixture_reference) != {"available"}:
                raise AttributionError("unavailable fixture_reference is not exact")
        elif fixture_reference.get("available") is True:
            if set(fixture_reference) != {
                "available", "metric_class", "n", "signature_sha256",
                "provider_free_gate_passed", "provider_calls", "judge_calls",
            }:
                raise AttributionError("fixture_reference contains an unknown field")
            if fixture_reference.get("metric_class") != "synthetic_fixture_reference":
                raise AttributionError("fixture_reference metric class is invalid")
            _positive_int(fixture_reference.get("n"), "fixture_reference.n")
            _sha(fixture_reference.get("signature_sha256"), "fixture_reference.signature_sha256")
            _bool(fixture_reference.get("provider_free_gate_passed"), "fixture_reference.provider_free_gate_passed")
            if fixture_reference.get("provider_calls") != 0 or fixture_reference.get("judge_calls") != 0:
                raise AttributionError("fixture_reference contains provider activity")
        else:
            raise AttributionError("fixture_reference.available must be boolean")

    if "selected_slice_recovery_reference" in report:
        selected_reference = report["selected_slice_recovery_reference"]
        if not isinstance(selected_reference, Mapping):
            raise AttributionError("selected_slice_recovery_reference is malformed")
        if selected_reference.get("available") is False:
            if set(selected_reference) not in ({"available"}, {"available", "metric_class"}):
                raise AttributionError("unavailable selected-slice reference is not exact")
            if "metric_class" in selected_reference and selected_reference.get("metric_class") != "selected_slice_recovery":
                raise AttributionError("unavailable selected-slice reference metric class is invalid")
        elif selected_reference.get("available") is True:
            if set(selected_reference) != {
                "available", "metric_class", "n", "artifact_sha256",
                "provider_calls", "judge_calls", "claim_boundary",
            }:
                raise AttributionError("selected-slice reference contains an unknown field")
            if selected_reference.get("metric_class") != "selected_slice_recovery":
                raise AttributionError("selected-slice reference metric class is invalid")
            _positive_int(selected_reference.get("n"), "selected-slice reference.n")
            _sha(selected_reference.get("artifact_sha256"), "selected-slice reference.artifact_sha256")
            if selected_reference.get("provider_calls") != 0 or selected_reference.get("judge_calls") != 0:
                raise AttributionError("selected-slice reference contains provider activity")
            if not isinstance(selected_reference.get("claim_boundary"), str) or not selected_reference["claim_boundary"].strip():
                raise AttributionError("selected-slice reference claim boundary is invalid")
        else:
            raise AttributionError("selected-slice reference availability must be boolean")

    if "judged_qa_reference" in report:
        judged_reference = report["judged_qa_reference"]
        if not isinstance(judged_reference, Mapping) or set(judged_reference) != {
            "metric_class", "source_status", "answerer_model", "judge_model", "systems", "claim",
        }:
            raise AttributionError("judged_qa_reference is not exact")
        if judged_reference.get("metric_class") != "judged_qa_reference":
            raise AttributionError("judged QA reference metric class is invalid")
        for field in ("source_status", "answerer_model", "judge_model", "claim"):
            _nonempty_text(judged_reference.get(field), f"judged_qa_reference.{field}")
        if not isinstance(judged_reference.get("systems"), Mapping) or set(judged_reference["systems"]) != {
            "fullcontext", "oracle", "perseus-vault", "stateless",
        }:
            raise AttributionError("judged QA reference systems are not exact")
        for system, summary in judged_reference["systems"].items():
            if not isinstance(summary, Mapping) or set(summary) != {"n", "correct", "accuracy"}:
                raise AttributionError(f"judged QA reference system {system} is malformed")
            n = _positive_int(summary.get("n"), f"judged_qa_reference.systems.{system}.n")
            correct = _nonnegative_int(summary.get("correct"), f"judged_qa_reference.systems.{system}.correct")
            accuracy = summary.get("accuracy")
            if isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)) or not math.isfinite(float(accuracy)) or not 0 <= float(accuracy) <= 1:
                raise AttributionError(f"judged_qa_reference.systems.{system}.accuracy is invalid")
            if correct > n:
                raise AttributionError(f"judged_qa_reference.systems.{system}.correct exceeds n")

    if "selection_recovery" in report:
        expected_selection = _reference_selection(cases)
        if report["selection_recovery"] != expected_selection:
            raise AttributionError("selection recovery does not match cases")
    expected_candidate = (
        _candidate_gate(cases, report.get("fixture_reference"))
        if "fixture_reference" in report
        else _fixture_candidate_gate(cases)
    )
    if report.get("candidate_gate") != expected_candidate:
        raise AttributionError("candidate gate does not match projected cases")
    candidate = report.get("candidate_gate")
    if not isinstance(candidate, Mapping):
        raise AttributionError("candidate_gate is malformed")
    if not isinstance(candidate.get("provider_free_regression_test"), str):
        raise AttributionError("candidate gate must name a provider-free regression test")
    if candidate.get("paid_canary_authorized") is not False or candidate.get("paid_canary_started") is not False:
        raise AttributionError("report cannot authorize or start a paid canary")
    _scan_public_keys(report)
    without_hashes = {key: value for key, value in report.items() if key not in {"projection_sha256", "signature_sha256"}}
    expected_projection = sha256_json(without_hashes)
    if report.get("projection_sha256") != expected_projection:
        raise AttributionError("report projection digest mismatch")
    with_projection = {**without_hashes, "projection_sha256": expected_projection}
    if report.get("signature_sha256") != sha256_json(with_projection):
        raise AttributionError("report signature digest mismatch")


def _read_replay_payload(path: str | Path) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AttributionError(f"cannot load retrieval replay: {path.name}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        rows: list[Any] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AttributionError(
                    f"retrieval replay line {line_number} is not JSON"
                ) from exc
        if not rows:
            raise AttributionError("retrieval replay contains no JSON rows")
        return rows


def _hash_only_dataset_index(dataset: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(dataset, list) or not dataset:
        raise AttributionError("dataset must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for index, instance in enumerate(dataset):
        if not isinstance(instance, Mapping):
            raise AttributionError(f"dataset row {index} is not an object")
        question_id = _id(instance.get("question_id"), f"dataset row {index}.question_id")
        if question_id in result:
            raise AttributionError(f"dataset question IDs are not unique: {question_id}")
        question_type = _id(instance.get("question_type"), f"dataset row {index}.question_type")
        session_ids = instance.get("haystack_session_ids")
        if not isinstance(session_ids, list) or not session_ids:
            raise AttributionError(f"dataset row {index} has no haystack session IDs")
        normalized_sessions = [_id(value, f"dataset row {index}.session_id") for value in session_ids]
        evidence = instance.get("answer_session_ids")
        if not isinstance(evidence, list) or not evidence:
            raise AttributionError(f"dataset row {index} has no answer session IDs")
        evidence_ids = [_id(value, f"dataset row {index}.answer_session_id") for value in evidence]
        if len(evidence_ids) != len(set(evidence_ids)) or not set(evidence_ids) <= set(normalized_sessions):
            raise AttributionError(f"dataset row {index} answer sessions are not aligned")
        candidates: dict[str, tuple[str, str]] = {}
        for session_id in dict.fromkeys(normalized_sessions):
            identity = _ATTR_replay_sha256_text(session_id)
            candidate_hash = _ATTR_replay_sha256_text("candidate-" + identity)
            source_hash = _ATTR_replay_sha256_text("source-" + identity)
            prior = candidates.get(candidate_hash)
            if prior is not None and prior != (session_id, source_hash):
                raise AttributionError(f"dataset row {index} has a conflicting candidate hash mapping")
            candidates[candidate_hash] = (session_id, source_hash)
        result[question_id] = {
            "question_type": question_type,
            "evidence": evidence_ids,
            "candidates": candidates,
        }
    return result


def load_retrieval_replay(
    path: str | Path,
    dataset: list[Mapping[str, Any]] | None = None,
    *,
    expected_cases: int = _ATTR_EXPECTED_CASES,
    expected_depth: int = _ATTR_DEFAULT_RETRIEVAL_DEPTH,
) -> dict[str, Any]:
    # Load either the legacy projection or the current hash-only JSONL replay.
    payload = _read_replay_payload(path)
    if isinstance(payload, Mapping):
        return dict(payload)
    if not isinstance(payload, list):
        raise AttributionError("retrieval replay must be an object or JSONL object list")
    if dataset is None:
        raise AttributionError("hash-only JSONL replay requires the local dataset")
    _positive_int(expected_cases, "expected_cases")
    _positive_int(expected_depth, "expected_depth")
    dataset_index = _hash_only_dataset_index(dataset)
    if len(payload) != expected_cases or len(dataset_index) != expected_cases:
        raise AttributionError("hash-only replay and dataset case counts do not match")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, envelope in enumerate(payload):
        if not isinstance(envelope, Mapping):
            raise AttributionError(f"retrieval replay row {index} is not an object")
        try:
            _ATTR_validate_replay_envelope(dict(envelope))
        except _ATTR_ReplayValidationError as exc:
            raise AttributionError(f"retrieval replay row {index} is invalid") from exc
        request = envelope.get("request")
        qid = _id(request.get("cell_id"), f"retrieval replay row {index}.cell_id") if isinstance(request, Mapping) else None
        if qid is None or qid in seen or qid not in dataset_index:
            raise AttributionError(f"retrieval replay row {index} is not aligned to the dataset")
        retrieval = envelope.get("retrieval")
        membership = envelope.get("membership")
        if not isinstance(retrieval, Mapping) or retrieval.get("mode") != "hybrid":
            raise AttributionError(f"retrieval replay row {index} is not hybrid")
        if envelope.get("status") != "complete" or not isinstance(membership, Mapping) or membership.get("complete") is not True:
            raise AttributionError(f"retrieval replay row {index} is not complete")
        if retrieval.get("top_k", 0) < expected_depth:
            raise AttributionError(f"retrieval replay row {index} is shallower than the attribution depth")
        candidates = envelope.get("candidates")
        if not isinstance(candidates, list) or len(candidates) < expected_depth:
            raise AttributionError(f"retrieval replay row {index} has too few delivered candidates")
        ordered = sorted(candidates, key=lambda item: item.get("final_rank", 0))
        top: list[str] = []
        for candidate_index, candidate in enumerate(ordered):
            candidate_hash = candidate.get("candidate_id_sha256")
            mapped = dataset_index[qid]["candidates"].get(candidate_hash)
            if mapped is None:
                raise AttributionError(f"retrieval replay row {index} contains an unmapped candidate")
            session_id, source_hash = mapped
            if candidate.get("source_ref_sha256") != source_hash:
                raise AttributionError(f"retrieval replay row {index} source reference mismatch")
            if candidate.get("final_rank") != candidate_index + 1:
                raise AttributionError(f"retrieval replay row {index} final ranks are not contiguous")
            top.append(session_id)
        normalized.append(
            {
                "question_id": qid,
                "question_type": dataset_index[qid]["question_type"],
                "evidence": list(dataset_index[qid]["evidence"]),
                "modes": {"hybrid": {"top": top}},
            }
        )
        seen.add(qid)
    if seen != set(dataset_index):
        raise AttributionError("hash-only replay does not cover the dataset")
    return {"offline": True, "n_instances": len(normalized), "per_question": sorted(normalized, key=lambda row: row["question_id"])}

def _load_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AttributionError(f"cannot load JSON artifact: {Path(path).name}") from exc


def _selected_slice_reference(path: str | Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, Mapping):
        raise AttributionError("selected-slice reference must be an object")
    n = payload.get("n_cases")
    if not isinstance(n, int) or n <= 0:
        n = len(payload.get("cases", [])) if isinstance(payload.get("cases"), list) else 0
    if n <= 0:
        raise AttributionError("selected-slice reference has no bounded case count")
    return {
        "available": True,
        "metric_class": "selected_slice_recovery",
        "n": n,
        "artifact_sha256": sha256_file(path),
        "provider_calls": 0,
        "judge_calls": 0,
        "claim_boundary": "prior selected-slice recovery reference; not a 500-case QA metric",
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    validate_report(report)
    summary = report["provider_free_attribution"]["summary"]
    lines = [
        "# LongMemEval provider-free failure attribution (#1132)",
        "",
        "- **Status:** completed provider-free gate",
        f"- **Cases:** {summary['n']}",
        f"- **Attributable Oracle-right/Vault-wrong cases:** {summary['attributable_failure_count']}",
        "- **Provider calls:** 0",
        "- **Judge calls:** 0",
        "- **Raw prompts/responses/memory bodies:** not captured",
        "",
        "## Claim boundary",
        "",
        report["claim_boundary"],
        "",
        "## Primary attribution counts",
        "",
        "| reason | cases |",
        "|---|---:|",
    ]
    for reason, count in summary["primary_reason_counts"].items():
        lines.append(f"| `{reason}` | {count} |")
    lines.extend([
        "",
        "## Key bounded observations",
        "",
        f"- Required evidence selected at rank <= {report['selection_recovery']['retrieval_depth']}: {report['selection_recovery']['all_required_selected']}/{report['selection_recovery']['n']}.",
        f"- Evidence-present failure cases: {report['candidate_gate']['evidence_present_failure_count']}.",
        f"- Answer-synthesis candidate flags among evidence-present failures: {summary['reason_code_counts'].get('answer_synthesis_candidate', 0)}.",
        f"- Selected-context effect flags: {summary['reason_code_counts'].get('selected_context_effect', 0)}.",
        "- No default full-context source-token loss was observed among selected required evidence; the budget-pressure flag is telemetry because the frozen full path does not enforce the ranked-snippet budget.",
        "",
        "## Metric separation",
        "",
        "- `judged_qa_reference`: the accepted frozen-default 500-case answer/judge result, copied as a historical reference only.",
        "- `provider_free_selection_recovery`: deterministic required-evidence membership/rank recovery from the sanitized replay.",
        "- `provider_free_attribution`: deterministic flags and reason codes; not a new QA accuracy score.",
        "- `selected_slice_recovery_reference`: prior 63-case selected-slice evidence, kept separate from this denominator.",
        "",
        "## Candidate disposition",
        "",
        f"- **Candidate identified:** {report['candidate_gate']['candidate_identified']}",
        f"- **Specific testable candidate:** `{report['candidate_gate']['specific_testable_candidate']}`",
        "- **Paid canary authorized:** false",
        "- **Paid canary started:** false",
        "- A paired paid canary remains a separate authorization decision.",
        "",
        "## Artifact commitments",
        "",
        f"- Projection SHA-256: `{report['projection_sha256']}`",
        f"- Signature SHA-256: `{report['signature_sha256']}`",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provider-free LongMemEval failure attribution gate")
    parser.add_argument("--baseline", required=True, help="accepted sanitized 500-case baseline JSON")
    parser.add_argument("--retrieval-replay", required=True, help="sanitized 500-case retrieval replay JSON")
    parser.add_argument("--data", required=True, help="local LongMemEval dataset used for source features")
    parser.add_argument("--fixture", required=True, help="committed synthetic attribution fixture")
    parser.add_argument("--selected-slice", default=None, help="prior sanitized selected-slice reference")
    parser.add_argument("--out", required=True, help="bounded JSON report path")
    parser.add_argument("--markdown-out", default=None, help="human-readable report path")
    parser.add_argument("--expected-cases", type=int, default=_ATTR_EXPECTED_CASES)
    parser.add_argument("--retrieval-depth", type=int, default=_ATTR_DEFAULT_RETRIEVAL_DEPTH)
    parser.add_argument("--context-budget-tokens", type=int, default=_ATTR_DEFAULT_CONTEXT_BUDGET)
    args = parser.parse_args(argv)
    try:
        baseline = _load_json(args.baseline)
        dataset = _load_json(args.data)
        replay = load_retrieval_replay(
            args.retrieval_replay,
            dataset,
            expected_cases=args.expected_cases,
            expected_depth=args.retrieval_depth,
        )
        fixture_cases = load_synthetic_fixture(args.fixture)
        fixture_report = build_fixture_report(fixture_cases)
        source_artifacts = [
            _artifact("accepted_baseline", sha256_file(args.baseline), n=args.expected_cases * 4),
            _artifact("sanitized_retrieval_replay", sha256_file(args.retrieval_replay), n=args.expected_cases),
            _artifact("longmemeval_dataset", sha256_file(args.data), n=args.expected_cases),
            _artifact("synthetic_fixture", sha256_file(args.fixture), n=len(fixture_cases)),
            _artifact("synthetic_fixture_report_signature", fixture_report["signature_sha256"], n=len(fixture_cases)),
        ]
        selected_reference = _selected_slice_reference(args.selected_slice) if args.selected_slice else None
        if selected_reference:
            source_artifacts.append(
                _artifact("selected_slice_reference", selected_reference["artifact_sha256"], n=selected_reference["n"])
            )
        report = build_attribution_report(
            baseline,
            replay,
            dataset,
            source_artifacts=source_artifacts,
            fixture_reference={
                "available": True,
                "metric_class": "synthetic_fixture_reference",
                "n": len(fixture_cases),
                "signature_sha256": fixture_report["signature_sha256"],
                "provider_free_gate_passed": fixture_report["candidate_gate"]["provider_free_gate_passed"],
                "provider_calls": 0,
                "judge_calls": 0,
            },
            selected_slice_reference=selected_reference,
            expected_cases=args.expected_cases,
            retrieval_depth=args.retrieval_depth,
            context_budget_tokens=args.context_budget_tokens,
        )
        validate_report(report)
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if args.markdown_out:
            markdown = Path(args.markdown_out)
            markdown.parent.mkdir(parents=True, exist_ok=True)
            markdown.write_text(render_markdown(report), encoding="utf-8")
        summary = report["provider_free_attribution"]["summary"]
        print(
            f"ATTRIBUTION_GATE {summary['n']}/{args.expected_cases} "
            f"failures={summary['attributable_failure_count']} "
            f"provider_calls=0 judge_calls=0 report={output}"
        )
        print(f"ATTRIBUTION_SIGNATURE {report['signature_sha256']}")
        return 0
    except AttributionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
