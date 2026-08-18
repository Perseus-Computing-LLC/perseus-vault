"""Protocol and offline adapters for the official BEAM task lane.

The existing ``benchmark/beam`` suite measures Vault correctness and
retrieval determinism while growing an inert filler corpus.  This module is a
separate boundary for the public BEAM conversation/probing-question task.  It
contains no provider SDKs and never places raw questions, rubrics, answers, or
memory bodies in the public projection.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable

PROTOCOL_SCHEMA = "perseus-vault-beam-task/v1"
REPORT_SCHEMA = "perseus-vault-beam-task-report/v1"
SOURCE_REPOSITORY = "https://github.com/mohammadtavakoli78/BEAM"
SIZES = ("100K", "500K", "1M", "10M")
ABILITY_TYPES = (
    "abstention",
    "contradiction_resolution",
    "event_ordering",
    "information_extraction",
    "instruction_following",
    "knowledge_update",
    "multi_session_reasoning",
    "preference_following",
    "summarization",
    "temporal_reasoning",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MESSAGE_REF_RE = re.compile(r"\b(?:chat(?:[_ ]id)?|session)\s*[:#-]?\s*(\d+)\b", re.I)

NOT_MEASURED_MODEL = {
    "status": "not_measured",
    "model": "not_measured",
    "prompt_id": "not_measured",
    "prompt_sha256": "not_measured",
    "temperature": 0.0,
}


def stable_json(value: Any) -> str:
    """Return canonical JSON and reject non-finite numeric values."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def digest_manifest(value: Any) -> str:
    return sha256_text(stable_json(value))


def estimate_tokens(text: Any) -> int:
    """Conservative provider-neutral estimate used only for budget telemetry."""
    return max(1, (len(str(text)) + 3) // 4)


def _require_public_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{field} must be a bounded identifier")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
        raise ValueError(f"{field} must be a bounded identifier")
    if any(token in value.lower() for token in ("token", "secret", "password", "credential", "private")):
        raise ValueError(f"{field} contains a forbidden private marker")
    return value


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _copy_model(value: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(value)


def _validate_model(value: Any, field: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    status = value.get("status")
    if status not in {"measured", "not_measured", "unavailable", "failed"}:
        raise ValueError(f"{field}.status is invalid")
    _require_public_id(value.get("model"), f"{field}.model")
    _require_public_id(value.get("prompt_id"), f"{field}.prompt_id")
    if status == "measured":
        _require_sha(value.get("prompt_sha256"), f"{field}.prompt_sha256")
    elif value.get("prompt_sha256") != "not_measured":
        _require_sha(value.get("prompt_sha256"), f"{field}.prompt_sha256")
    temperature = value.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not math.isfinite(float(temperature)):
        raise ValueError(f"{field}.temperature must be finite")
    if temperature < 0:
        raise ValueError(f"{field}.temperature must be non-negative")


def default_run_config() -> dict[str, Any]:
    return {
        "protocol_schema": PROTOCOL_SCHEMA,
        "retrieval": {"mode": "hybrid", "top_k": 10},
        "answerer": _copy_model(NOT_MEASURED_MODEL),
        "judge": _copy_model(NOT_MEASURED_MODEL),
        "retry_policy": {"max_attempts": 3, "backoff_seconds": 0.0},
        "token_budget": {
            "context_tokens": 12_000,
            "answerer_output_tokens": 1_200,
            "judge_input_tokens": 4_000,
        },
    }


def validate_run_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("run config must be an object")
    if config.get("protocol_schema") not in (None, PROTOCOL_SCHEMA):
        raise ValueError("unsupported protocol schema")
    retrieval = config.get("retrieval")
    if not isinstance(retrieval, dict) or retrieval.get("mode") not in {"fts5", "dense", "hybrid", "auto"}:
        raise ValueError("retrieval.mode is invalid")
    top_k = retrieval.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 1000:
        raise ValueError("retrieval.top_k must be between 1 and 1000")
    _validate_model(config.get("answerer"), "answerer")
    _validate_model(config.get("judge"), "judge")
    retry = config.get("retry_policy")
    if not isinstance(retry, dict):
        raise ValueError("retry_policy must be an object")
    attempts = retry.get("max_attempts")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 20:
        raise ValueError("retry_policy.max_attempts must be between 1 and 20")
    backoff = retry.get("backoff_seconds", 0.0)
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or not math.isfinite(float(backoff)) or backoff < 0:
        raise ValueError("retry_policy.backoff_seconds must be finite and non-negative")
    budgets = config.get("token_budget", {})
    if not isinstance(budgets, dict):
        raise ValueError("token_budget must be an object")
    for key in ("context_tokens", "answerer_output_tokens", "judge_input_tokens"):
        value = budgets.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"token_budget.{key} must be positive")


def _extract_message_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, bool) or value is None:
        return refs
    if isinstance(value, int):
        refs.add(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            refs.add(str(int(text)))
        for match in _MESSAGE_REF_RE.finditer(text):
            refs.add(str(int(match.group(1))))
    elif isinstance(value, dict):
        for child in value.values():
            refs.update(_extract_message_refs(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            refs.update(_extract_message_refs(child))
    return refs


def flatten_chat(chat: Any) -> list[dict[str, Any]]:
    """Flatten the official ``chat.json`` batch/turn shape to message rows."""
    if not isinstance(chat, list):
        raise ValueError("chat.json must contain a list")
    messages: list[dict[str, Any]] = []
    fallback_id = 0
    for batch in chat:
        if not isinstance(batch, dict) or not isinstance(batch.get("turns"), list):
            raise ValueError("chat batch must contain turns")
        for turn_group in batch["turns"]:
            if not isinstance(turn_group, list):
                raise ValueError("chat turn group must be a list")
            for message in turn_group:
                if not isinstance(message, dict):
                    raise ValueError("chat message must be an object")
                content = message.get("content")
                if not isinstance(content, str):
                    raise ValueError("chat message content must be a string")
                role = message.get("role")
                if not isinstance(role, str) or not role:
                    raise ValueError("chat message role must be a string")
                raw_id = message.get("id", fallback_id)
                key = str(raw_id)
                row = {
                    "id": key,
                    "role": role,
                    "content": content,
                }
                for optional in ("time_anchor", "index", "question_type"):
                    if optional in message:
                        row[optional] = message[optional]
                messages.append(row)
                fallback_id += 1
    if not messages:
        raise ValueError("chat.json contains no messages")
    return messages


def _resolve_data_root(data_root: Path) -> Path:
    """Accept either the upstream checkout or its ``chats`` directory."""
    if (data_root / "chats").is_dir() and not any((data_root / size).is_dir() for size in SIZES):
        return data_root / "chats"
    return data_root


def _conversation_dirs(data_root: Path, size: str) -> list[Path]:
    data_root = _resolve_data_root(data_root)
    if size not in SIZES:
        raise ValueError(f"unsupported BEAM size: {size}")
    root = data_root / size
    if not root.is_dir():
        raise ValueError(f"missing BEAM size directory: {root}")
    paths = [path for path in root.iterdir() if path.is_dir()]
    return sorted(paths, key=lambda path: (0, int(path.name)) if path.name.isdigit() else (1, path.name))


def _load_conversation(data_root: Path, size: str, conversation_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chat_path = conversation_dir / "chat.json"
    question_path = conversation_dir / "probing_questions" / "probing_questions.json"
    if not chat_path.is_file() or not question_path.is_file():
        raise ValueError(f"incomplete BEAM conversation: {conversation_dir}")
    try:
        chat = json.loads(chat_path.read_text(encoding="utf-8"))
        questions = json.loads(question_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {conversation_dir}: {exc}") from exc
    messages = flatten_chat(chat)
    if not isinstance(questions, dict):
        raise ValueError("probing_questions.json must be an object")
    return messages, questions


def normalize_question(*, size: str, conversation_id: str, ability: str, index: int,
                       raw: dict[str, Any], message_ids: set[str]) -> dict[str, Any]:
    """Normalize one official probing question while checking split integrity."""
    if size not in SIZES:
        raise ValueError(f"unsupported BEAM size: {size}")
    _require_public_id(conversation_id, "conversation_id")
    if ability not in ABILITY_TYPES:
        raise ValueError(f"unknown BEAM ability: {ability}")
    if not isinstance(index, int) or index < 0:
        raise ValueError("question index must be non-negative")
    if not isinstance(raw, dict):
        raise ValueError("question must be an object")
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question text is required")
    rubric = raw.get("rubric")
    if not isinstance(rubric, list) or not rubric or any(not isinstance(item, str) or not item.strip() for item in rubric):
        raise ValueError("question rubric must be a non-empty string list")
    gold = next((raw.get(key) for key in ("answer", "ideal_answer", "ideal_response", "ideal_summary") if isinstance(raw.get(key), str) and raw[key].strip()), None)
    if gold is None and ability not in {"instruction_following", "preference_following"}:
        raise ValueError("question gold answer/response is required")
    source_ids = _extract_message_refs(raw.get("source_chat_ids"))
    source_ids.update(_extract_message_refs(raw.get("conversation_references")))
    known_message_ids = {str(value) for value in message_ids}
    unknown = sorted(source_ids - known_message_ids, key=lambda value: (len(value), value))
    if unknown:
        raise ValueError(f"source message IDs outside conversation: {unknown[:3]}")
    identity = {
        "size": size,
        "conversation_id": conversation_id,
        "ability": ability,
        "index": index,
    }
    question_id = f"beam-{sha256_text(stable_json(identity))[:32]}"
    return {
        "question_id": question_id,
        "size": size,
        "conversation_id": conversation_id,
        "ability": ability,
        "index": index,
        "question": question.strip(),
        "gold": gold.strip() if isinstance(gold, str) else None,
        "rubric": [item.strip() for item in rubric],
        "source_message_ids": sorted(source_ids, key=lambda value: (len(value), value)),
        "difficulty": raw.get("difficulty", "unspecified"),
        "messages": [],
    }


def load_cases(data_root: str | Path, *, size: str, conversation_ids: Iterable[str] | None = None,
               question_types: Iterable[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    """Load official BEAM files or the committed provider-free fixture."""
    root = _resolve_data_root(Path(data_root))
    allowed_ids = {str(value) for value in conversation_ids} if conversation_ids is not None else None
    allowed_abilities = set(question_types) if question_types is not None else None
    if allowed_abilities is not None and not allowed_abilities.issubset(set(ABILITY_TYPES)):
        raise ValueError("question_types contains an unknown ability")
    cases: list[dict[str, Any]] = []
    for conversation_dir in _conversation_dirs(root, size):
        conversation_id = conversation_dir.name
        if allowed_ids is not None and conversation_id not in allowed_ids:
            continue
        messages, questions = _load_conversation(root, size, conversation_dir)
        message_ids = {message["id"] for message in messages}
        for ability in ABILITY_TYPES:
            if ability not in questions:
                continue
            if allowed_abilities is not None and ability not in allowed_abilities:
                continue
            rows = questions[ability]
            if not isinstance(rows, list):
                raise ValueError(f"{ability} questions must be a list")
            for index, raw in enumerate(rows):
                case = normalize_question(
                    size=size,
                    conversation_id=conversation_id,
                    ability=ability,
                    index=index,
                    raw=raw,
                    message_ids=message_ids,
                )
                case["messages"] = copy.deepcopy(messages)
                cases.append(case)
                if limit is not None and len(cases) >= limit:
                    return cases
    if not cases:
        raise ValueError(f"no BEAM cases selected for {size}")
    return cases


def build_manifest(*, data_root: str | Path, sizes: Iterable[str], source_revision: str,
                   retrieval: dict[str, Any], answerer: dict[str, Any], judge: dict[str, Any],
                   conversation_ids: Iterable[str] | None = None,
                   question_types: Iterable[str] | None = None) -> dict[str, Any]:
    """Create a content-bound protocol manifest; floating revisions are refused."""
    if not isinstance(source_revision, str) or not _REVISION_RE.fullmatch(source_revision):
        raise ValueError("source revision must be a 40-character commit revision")
    selected_sizes = list(sizes)
    if not selected_sizes or any(size not in SIZES for size in selected_sizes):
        raise ValueError("sizes must be non-empty BEAM size names")
    selected_conversation_ids = (
        sorted({str(value) for value in conversation_ids})
        if conversation_ids is not None
        else None
    )
    selected_question_types = (
        sorted({str(value) for value in question_types})
        if question_types is not None
        else None
    )
    if selected_question_types is not None and not set(selected_question_types).issubset(set(ABILITY_TYPES)):
        raise ValueError("question_types contains an unknown ability")
    config = default_run_config()
    config["retrieval"] = copy.deepcopy(retrieval)
    config["answerer"] = copy.deepcopy(answerer)
    config["judge"] = copy.deepcopy(judge)
    validate_run_config(config)
    root = _resolve_data_root(Path(data_root))
    files: list[dict[str, str]] = []
    for size in selected_sizes:
        for conversation_dir in _conversation_dirs(root, size):
            if selected_conversation_ids is not None and conversation_dir.name not in set(selected_conversation_ids):
                continue
            for path in (conversation_dir / "chat.json", conversation_dir / "probing_questions" / "probing_questions.json"):
                if not path.is_file():
                    raise ValueError(f"missing source file: {path}")
                files.append({
                    "path": str(path.relative_to(root)).replace("\\", "/"),
                    "sha256": sha256_file(path),
                })
    if not files:
        raise ValueError("manifest selection contains no source files")
    files.sort(key=lambda item: item["path"])
    return {
        "schema_version": PROTOCOL_SCHEMA,
        "source": {
            "repository": SOURCE_REPOSITORY,
            "revision": source_revision,
            "revision_policy": "immutable-commit-only",
        },
        "selection": {
            "sizes": selected_sizes,
            "conversation_ids": selected_conversation_ids if selected_conversation_ids is not None else "all",
            "question_types": selected_question_types if selected_question_types is not None else list(ABILITY_TYPES),
        },
        "retrieval": copy.deepcopy(retrieval),
        "answerer": copy.deepcopy(answerer),
        "judge": copy.deepcopy(judge),
        "preprocessing": {
            "chat_reader": "official-chat-json-batches-v1",
            "question_reader": "official-probing-questions-json-v1",
            "token_estimate": "characters-div-four-ceil",
            "raw_inputs_public": False,
        },
        "source_files": files,
    }


def validate_retrieval_artifact(artifact: dict[str, Any]) -> None:
    if not isinstance(artifact, dict):
        raise ValueError("retrieval artifact must be an object")
    if artifact.get("complete") is not True:
        raise ValueError("retrieval artifact must be complete")
    top_k = artifact.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("retrieval artifact top_k must be positive")
    ranked = artifact.get("ranked")
    if not isinstance(ranked, list) or len(ranked) > top_k:
        raise ValueError("retrieval artifact ranked list is invalid")
    for index, item in enumerate(ranked, 1):
        if not isinstance(item, dict) or item.get("rank") != index:
            raise ValueError("retrieval ranks must be contiguous")
        _require_sha(item.get("key_sha256"), "retrieval key_sha256")
        _require_sha(item.get("content_sha256"), "retrieval content_sha256")
        score = item.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
            raise ValueError("retrieval score must be finite")
    if not _SHA256_RE.fullmatch(str(artifact.get("evidence_sha256", ""))):
        raise ValueError("retrieval evidence digest is invalid")


def make_retrieval_artifact(case: dict[str, Any], ranked: list[dict[str, Any]], *, top_k: int) -> dict[str, Any]:
    if not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be positive")
    public_ranked: list[dict[str, Any]] = []
    for index, item in enumerate(ranked[:top_k], 1):
        if not isinstance(item, dict):
            raise ValueError("retrieval result must be an object")
        key = str(item.get("key", ""))
        content = str(item.get("content", ""))
        if not key or not content:
            raise ValueError("retrieval result key and content are required")
        score = item.get("score", 0.0)
        public_ranked.append({
            "rank": index,
            "key_sha256": sha256_text(key),
            "content_sha256": sha256_text(content),
            "content_chars": len(content),
            "score": round(float(score), 8),
        })
    artifact = {
        "complete": True,
        "top_k": top_k,
        "ranked": public_ranked,
        "candidate_count": len(ranked),
        "evidence_sha256": sha256_text(stable_json(public_ranked)),
    }
    validate_retrieval_artifact(artifact)
    return artifact


def project_case(case: dict[str, Any], artifact: dict[str, Any], *, status: str = "retrieved") -> dict[str, Any]:
    validate_retrieval_artifact(artifact)
    if status not in {"retrieved", "not_measured", "failed"}:
        raise ValueError("invalid case status")
    messages = case.get("messages") or []
    source_digest = sha256_text(stable_json([
        {"id": str(row.get("id")), "role": row.get("role"), "content_sha256": sha256_text(str(row.get("content", "")))}
        for row in messages
    ]))
    question_budget = estimate_tokens(case.get("question", ""))
    retrieved_chars = sum(item.get("content_chars", 0) for item in artifact["ranked"])
    return {
        "question_id": case["question_id"],
        "ability": case["ability"],
        "status": status,
        "source_messages_sha256": source_digest,
        "retrieval": artifact,
        "token_budget": {
            "question_tokens_est": question_budget,
            "retrieved_context_tokens_est": estimate_tokens("x" * retrieved_chars) if retrieved_chars else 0,
            "answerer_input_tokens_est": question_budget + (estimate_tokens("x" * retrieved_chars) if retrieved_chars else 0),
            "judge_input_tokens_est": 0,
        },
        "answerer": {"status": "not_measured"},
        "judge": {"status": "not_measured"},
    }


def call_with_retries(fn: Callable[[], Any], *, max_attempts: int, backoff_seconds: float = 0.0,
                      sleep: Callable[[float], None] | None = None) -> dict[str, Any]:
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    if sleep is None:
        sleep = lambda _seconds: None
    last: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return {"status": "ok", "attempts": attempt, "value": fn()}
        except Exception as exc:  # provider boundary: record, do not mis-score
            last = exc
            if attempt < max_attempts and backoff_seconds:
                sleep(float(backoff_seconds) * (2 ** (attempt - 1)))
    assert last is not None
    return {
        "status": "error",
        "attempts": max_attempts,
        "error_class": type(last).__name__,
        "error": str(last)[:256],
    }


def _terms(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


class FixtureAdapter:
    """Deterministic provider-free retriever used by CI and protocol tests."""

    def __init__(self, messages: list[dict[str, Any]]):
        self._messages = copy.deepcopy(messages)

    def retrieve(self, question: str, *, top_k: int) -> list[dict[str, Any]]:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        query_terms = _terms(question)
        ranked = []
        for message in self._messages:
            content = str(message.get("content", ""))
            score = len(query_terms & _terms(content))
            ranked.append({
                "key": f"message-{message.get('id')}",
                "content": content,
                "score": float(score),
            })
        ranked.sort(key=lambda item: (-item["score"], item["key"]))
        selected = ranked[:top_k]
        return [{**item, "rank": index} for index, item in enumerate(selected, 1)]


def build_retrieval_report(*, manifest: dict[str, Any], config: dict[str, Any],
                           cases: list[dict[str, Any]], evidence_classes: dict[str, Any]) -> dict[str, Any]:
    validate_run_config(config)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != PROTOCOL_SCHEMA:
        raise ValueError("manifest does not use the BEAM task protocol")
    required_classes = {"vault_measured", "competitor_published", "competitor_reproduced"}
    if set(evidence_classes) != required_classes:
        raise ValueError("evidence classes must remain distinct")
    allowed_status = {"measured", "published", "reproduced", "not_measured", "unavailable", "not_comparable"}
    for name, value in evidence_classes.items():
        if not isinstance(value, dict) or value.get("status") not in allowed_status:
            raise ValueError(f"invalid evidence class: {name}")
    public_cases = sorted(copy.deepcopy(cases), key=lambda item: item.get("question_id", ""))
    for case in public_cases:
        if set(case) - {"question_id", "ability", "status", "source_messages_sha256", "retrieval", "token_budget", "answerer", "judge"}:
            raise ValueError("case contains raw or unknown public fields")
        if "question" in case or "gold" in case or "messages" in case:
            raise ValueError("raw task inputs cannot enter public report")
        if "retrieval" in case:
            validate_retrieval_artifact(case["retrieval"])
    ability_counts: dict[str, dict[str, int]] = {}
    for case in public_cases:
        ability = _require_public_id(case.get("ability"), "case.ability")
        status = _require_public_id(case.get("status"), "case.status")
        bucket = ability_counts.setdefault(ability, {"total": 0, "retrieved": 0, "not_measured": 0, "failed": 0})
        bucket["total"] += 1
        bucket[status] = bucket.get(status, 0) + 1
    report = {
        "schema_version": REPORT_SCHEMA,
        "benchmark_id": "beam-task",
        "protocol_schema": PROTOCOL_SCHEMA,
        "manifest": copy.deepcopy(manifest),
        "manifest_sha256": digest_manifest(manifest),
        "config_sha256": digest_manifest(config),
        "config": copy.deepcopy(config),
        "cases": public_cases,
        "by_ability": ability_counts,
        "evidence_classes": copy.deepcopy(evidence_classes),
        "status": "partial" if any(case.get("status") != "retrieved" for case in public_cases) else "retrieved",
        "raw_inputs_captured": False,
        "network_calls": 0,
        "not_measured": ["answerer", "judge", "end_to_end_qa_accuracy"],
    }
    report["result_signature_sha256"] = sha256_text(stable_json({
        "manifest_sha256": report["manifest_sha256"],
        "config_sha256": report["config_sha256"],
        "cases": report["cases"],
        "by_ability": report["by_ability"],
        "evidence_classes": report["evidence_classes"],
    }))
    report["custody_sha256"] = sha256_text(stable_json(report))
    return report


__all__ = [
    "ABILITY_TYPES", "FixtureAdapter", "NOT_MEASURED_MODEL", "PROTOCOL_SCHEMA",
    "REPORT_SCHEMA", "SIZES", "build_manifest", "build_retrieval_report",
    "call_with_retries", "default_run_config", "digest_manifest", "estimate_tokens",
    "flatten_chat", "load_cases", "make_retrieval_artifact", "normalize_question",
    "project_case", "sha256_file", "sha256_text", "stable_json", "validate_retrieval_artifact",
    "validate_run_config",
]
