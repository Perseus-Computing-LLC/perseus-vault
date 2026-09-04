"""Provider-free LongMemEval-V2 memory boundary for Perseus Vault.

The class in this module intentionally implements the small V2 method boundary
without importing the external harness or any model/provider SDK.  It is a
fresh in-process Vault-shaped store used only for synthetic preparation and
replay.  A real V2 run can wrap the same boundary around a separately governed
Vault process later; this module never opens a production database.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ADAPTER_SCHEMA_VERSION = "perseus-vault-longmemeval-v2-adapter/v1"
ADAPTER_MEMORY_TYPE = "perseus_vault_longmemeval_v2"
MAX_ID_CHARS = 128
MAX_SCOPE_CHARS = 128
MAX_TIMESTAMP_CHARS = 80
MAX_SOURCE_REFS = 16
MAX_SOURCE_REF_CHARS = 256
MAX_STORED_CONTENT_CHARS = 16_000
MAX_QUERY_CHARS = 8_192
MAX_TRAJECTORIES = 50_000
MAX_EVENTS_PER_TRAJECTORY = 10_000

_FORBIDDEN_BENCHMARK_FIELDS = frozenset(
    {
        "question_id",
        "question_type",
        "answer_session_ids",
        "gold_answer",
        "answer",
        "evaluator_metadata",
        "evaluator_config",
        "hidden_label",
        "label",
        "gold",
    }
)

_ACTIVE_LIFECYCLES = frozenset({"active", "fresh", "valid", "current"})
_INACTIVE_LIFECYCLES = frozenset(
    {"stale", "expired", "revoked", "quarantined", "tombstone", "tombstoned", "deprecated", "superseded"}
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


class AdapterContractError(ValueError):
    """Raised when a trajectory or adapter boundary is malformed."""


@dataclass(frozen=True)
class _EvidenceRecord:
    trajectory_id: str
    session_id: str
    event_id: str
    event_index: int
    state_index: int | None
    timestamp: str
    timestamp_order: int
    source_refs: tuple[str, ...]
    scope: str
    lifecycle: str
    conflict_ids: tuple[str, ...]
    superseded_by: str | None
    supersedes: tuple[str, ...]
    content: str
    content_sha256: str
    image_path: Path | None
    image_sha256: str | None

    @property
    def visible(self) -> bool:
        return self.lifecycle in _ACTIVE_LIFECYCLES and self.superseded_by is None


@dataclass
class _QueryDiagnostic:
    status: str = "abstained"
    reason: str = "no_evidence"
    query_sha256: str = ""
    query_image_sha256: str | None = None
    text_items: int = 0
    image_items: int = 0
    bounded: bool = True
    conflicts_visible: int = 0
    excluded_scope: int = 0
    excluded_lifecycle: int = 0
    excluded_superseded: int = 0
    missing_images: int = 0

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "query_sha256": self.query_sha256,
            "text_items": self.text_items,
            "image_items": self.image_items,
            "bounded": self.bounded,
            "conflicts_visible": self.conflicts_visible,
            "excluded": {
                "scope": self.excluded_scope,
                "lifecycle": self.excluded_lifecycle,
                "superseded": self.excluded_superseded,
                "missing_images": self.missing_images,
            },
        }
        if self.query_image_sha256 is not None:
            result["query_image_sha256"] = self.query_image_sha256
        return result


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _one_line(value: str, max_chars: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= max_chars:
        return value
    if max_chars <= 32:
        return value[:max_chars]
    marker = f" ...[truncated {len(value) - max_chars} chars]..."
    if len(marker) >= max_chars:
        return value[:max_chars]
    return value[: max_chars - len(marker)].rstrip() + marker


def _truncate_middle(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    marker = f"\n...[truncated {len(value) - max_chars} chars]...\n"
    if len(marker) >= max_chars:
        return value[:max_chars]
    left = (max_chars - len(marker)) // 2
    right = max_chars - len(marker) - left
    return value[:left].rstrip() + marker + value[-right:].lstrip()


def _identifier(value: Any, field: str, *, max_chars: int = MAX_ID_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterContractError(f"{field} must be non-empty text")
    value = value.strip()
    if len(value) > max_chars or "\x00" in value or "\n" in value or "\r" in value:
        raise AdapterContractError(f"{field} is unbounded or contains control characters")
    if max_chars <= MAX_ID_CHARS and _IDENTIFIER_RE.fullmatch(value) is None:
        raise AdapterContractError(f"{field} must be a bounded identifier")
    return value


def _optional_text(value: Any, field: str, *, max_chars: int = MAX_TIMESTAMP_CHARS) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AdapterContractError(f"{field} must be text or a number")
    text = str(value).strip()
    if not text or len(text) > max_chars or "\x00" in text or "\n" in text or "\r" in text:
        raise AdapterContractError(f"{field} is empty, unbounded, or contains control characters")
    return text


def _text_value(value: Any, field: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, Mapping):
        # Actions in the V2 trajectory schema are structured.  Only the small
        # display fields are admitted; arbitrary nested objects are not copied.
        pieces: list[str] = []
        for key in ("type", "name", "label", "target", "value", "text"):
            if key not in value or key in _FORBIDDEN_BENCHMARK_FIELDS:
                continue
            child = value[key]
            if isinstance(child, str) and child.strip():
                pieces.append(f"{key}={child.strip()}")
            elif isinstance(child, (int, float)) and not isinstance(child, bool):
                pieces.append(f"{key}={child}")
        return " ".join(pieces)
    return ""


def _text_parts(state: Mapping[str, Any], trajectory: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("state_text", "text", "observation", "action", "notes", "description"):
        if key not in state or key in _FORBIDDEN_BENCHMARK_FIELDS:
            continue
        value = _text_value(state[key], f"state.{key}").strip()
        if value:
            pieces.append(value)
    if not pieces and isinstance(trajectory.get("goal"), str):
        pieces.append(trajectory["goal"])
    return _one_line(" | ".join(pieces), MAX_STORED_CONTENT_CHARS)


def _string_list(value: Any, field: str, *, max_items: int, max_chars: int) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise AdapterContractError(f"{field} must be text or a list of text")
    if len(values) > max_items:
        raise AdapterContractError(f"{field} exceeds its bound")
    result: list[str] = []
    for index, item in enumerate(values):
        result.append(_identifier(item, f"{field}[{index}]", max_chars=max_chars))
    return tuple(result)


def _reference_list(value: Any, field: str) -> tuple[str, ...]:
    """Validate source references without restricting URLs to identifier syntax."""
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or len(values) > MAX_SOURCE_REFS:
        raise AdapterContractError(f"{field} must contain at most {MAX_SOURCE_REFS} references")
    result: list[str] = []
    for index, item in enumerate(values):
        text = _optional_text(item, f"{field}[{index}]", max_chars=MAX_SOURCE_REF_CHARS)
        if text is not None:
            result.append(text)
    return tuple(result)


def _timestamp_value(value: Any, field: str) -> tuple[str, int]:
    text = _optional_text(value, field)
    if text is None:
        return "unknown", 0
    # Preserve the original representation, while using a numeric projection
    # only for deterministic tie-breaking.  No wall clock is read.
    digits = re.sub(r"[^0-9]", "", text)
    try:
        order = int(digits[:18]) if digits else 0
    except ValueError:
        order = 0
    return text, order


def _lifecycle_value(value: Any, field: str) -> str:
    text = _optional_text(value, field, max_chars=MAX_ID_CHARS)
    if text is None:
        return "active"
    return text.lower()


def _resolve_image(
    value: Any,
    field: str,
    allowed_root: Path,
    trajectory_id: str | None = None,
) -> tuple[Path | None, str | None]:
    text = _optional_text(value, field, max_chars=1024)
    if text is None:
        return None, None
    candidate = Path(text)
    root = allowed_root.resolve()
    candidates = [candidate] if candidate.is_absolute() else [root / candidate]
    if not candidate.is_absolute() and trajectory_id is not None:
        candidates.extend(
            [root / trajectory_id / candidate, root / "trajectories" / trajectory_id / candidate]
        )
    for possible in candidates:
        try:
            resolved = possible.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved, _sha256_bytes(resolved.read_bytes())
    # Preserve a bounded missing-file diagnostic while still rejecting paths
    # that escape the configured root.
    try:
        resolved = candidates[0].resolve()
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AdapterContractError(f"{field} is outside the configured image root") from exc
    return resolved, None


def _event_id(trajectory_id: str, state: Mapping[str, Any], index: int) -> str:
    candidate = state.get("event_id")
    if candidate is None:
        candidate = state.get("step_id")
    if candidate is None:
        candidate = f"event-{index}"
    return _identifier(candidate, f"trajectory {trajectory_id} event_id")


def _state_sequence(trajectory: Mapping[str, Any], trajectory_id: str) -> list[Mapping[str, Any]]:
    states = trajectory.get("states")
    if states is None:
        states = trajectory.get("events", [])
    if not isinstance(states, list):
        raise AdapterContractError(f"trajectory {trajectory_id} states/events must be a list")
    if len(states) > MAX_EVENTS_PER_TRAJECTORY:
        raise AdapterContractError(f"trajectory {trajectory_id} has too many events")
    result: list[Mapping[str, Any]] = []
    for index, state in enumerate(states):
        if not isinstance(state, Mapping):
            raise AdapterContractError(f"trajectory {trajectory_id} event {index} is not an object")
        result.append(state)
    return result


class _VaultMemoryBackend:
    """Small deterministic, fresh-store projection used by the V2 adapter."""

    def __init__(self, params: Mapping[str, Any]) -> None:
        scope = params.get("scope", "global")
        self.scope = _identifier(scope, "scope", max_chars=MAX_SCOPE_CHARS)
        available = params.get("available", True)
        if not isinstance(available, bool):
            raise AdapterContractError("available must be boolean")
        self.available = available
        self.max_results = _positive_int(params.get("max_results", 8), "max_results", 128)
        self.max_text_chars = _positive_int(params.get("max_text_chars", 2_000), "max_text_chars", 8_192)
        self.max_total_text_chars = _positive_int(
            params.get("max_total_text_chars", 12_000), "max_total_text_chars", 65_536
        )
        self.max_image_items = _positive_int(params.get("max_image_items", 4), "max_image_items", 32)
        root = params.get("allowed_image_root", ".")
        if not isinstance(root, str) or not root.strip():
            raise AdapterContractError("allowed_image_root must be a non-empty path")
        self.allowed_image_root = Path(root).expanduser().resolve()
        self._records: dict[tuple[str, int, str], _EvidenceRecord] = {}
        self._trajectory_ids: set[str] = set()
        self._last_diagnostic = _QueryDiagnostic()

    @staticmethod
    def _positive_int(value: Any, field: str, maximum: int) -> int:
        return _positive_int(value, field, maximum)

    def insert(self, trajectory: Mapping[str, Any]) -> None:
        if not isinstance(trajectory, Mapping):
            raise AdapterContractError("trajectory must be an object")
        trajectory_id = trajectory.get("id", trajectory.get("trajectory_id"))
        trajectory_id = _identifier(trajectory_id, "trajectory.id")
        session_value = trajectory.get("session_id", trajectory.get("session"))
        session_id = _identifier(session_value or f"session-{trajectory_id}", "trajectory.session_id")
        trajectory_scope = _identifier(
            trajectory.get("scope", self.scope), "trajectory.scope", max_chars=MAX_SCOPE_CHARS
        )
        trajectory_lifecycle = _lifecycle_value(trajectory.get("lifecycle"), "trajectory.lifecycle")
        states = _state_sequence(trajectory, trajectory_id)
        if trajectory_id in self._trajectory_ids:
            self._records = {
                key: record for key, record in self._records.items() if record.trajectory_id != trajectory_id
            }
        self._trajectory_ids.add(trajectory_id)
        for index, state in enumerate(states):
            event_id = _event_id(trajectory_id, state, index)
            timestamp, timestamp_order = _timestamp_value(
                state.get("timestamp", state.get("timestamp_unix_ms")),
                f"trajectory {trajectory_id} event {index}.timestamp",
            )
            state_scope = _identifier(
                state.get("scope", trajectory_scope),
                f"trajectory {trajectory_id} event {index}.scope",
                max_chars=MAX_SCOPE_CHARS,
            )
            lifecycle = _lifecycle_value(state.get("lifecycle", trajectory_lifecycle), "state.lifecycle")
            source_refs_value = state.get(
                "source_refs", state.get("source_ref", trajectory.get("source_refs"))
            )
            if source_refs_value is None and state.get("url") is not None:
                source_refs_value = state.get("url")
            source_refs = _reference_list(
                source_refs_value,
                f"trajectory {trajectory_id} event {index}.source_refs",
            )
            conflict_ids = _string_list(
                state.get("conflict_ids", state.get("conflict_id")),
                f"trajectory {trajectory_id} event {index}.conflict_ids",
                max_items=16,
                max_chars=MAX_ID_CHARS,
            )
            superseded_by = _optional_text(
                state.get("superseded_by"),
                f"trajectory {trajectory_id} event {index}.superseded_by",
                max_chars=MAX_ID_CHARS,
            )
            if superseded_by is not None:
                superseded_by = _identifier(superseded_by, "superseded_by")
            supersedes = _string_list(
                state.get("supersedes"),
                f"trajectory {trajectory_id} event {index}.supersedes",
                max_items=16,
                max_chars=MAX_ID_CHARS,
            )
            content = _text_parts(state, trajectory)
            content_sha256 = _sha256_text(content)
            image_value = state.get("screenshot", state.get("image_path", state.get("image")))
            image_path, image_sha256 = _resolve_image(
                image_value,
                f"trajectory {trajectory_id} event {index}.image",
                self.allowed_image_root,
                trajectory_id,
            )
            state_index = state.get("state_index")
            if state_index is not None and (
                isinstance(state_index, bool) or not isinstance(state_index, int) or state_index < 0
            ):
                raise AdapterContractError(
                    f"trajectory {trajectory_id} event {index}.state_index must be non-negative"
                )
            key = (trajectory_id, index, event_id)
            self._records[key] = _EvidenceRecord(
                trajectory_id=trajectory_id,
                session_id=session_id,
                event_id=event_id,
                event_index=index,
                state_index=state_index,
                timestamp=timestamp,
                timestamp_order=timestamp_order,
                source_refs=source_refs,
                scope=state_scope,
                lifecycle=lifecycle,
                conflict_ids=conflict_ids,
                superseded_by=superseded_by,
                supersedes=supersedes,
                content=content,
                content_sha256=content_sha256,
                image_path=image_path,
                image_sha256=image_sha256,
            )
        if len(self._trajectory_ids) > MAX_TRAJECTORIES:
            raise AdapterContractError("trajectory store exceeds its bound")

    @property
    def memory_config(self) -> dict[str, Any]:
        return {
            "memory_type": ADAPTER_MEMORY_TYPE,
            "memory_params": {
                "scope": self.scope,
                "available": self.available,
                "max_results": self.max_results,
                "max_text_chars": self.max_text_chars,
                "max_total_text_chars": self.max_total_text_chars,
                "max_image_items": self.max_image_items,
            },
        }

    def query(self, query: str, query_image: str | None = None) -> list[dict[str, str]]:
        if not isinstance(query, str):
            raise AdapterContractError("query must be a string")
        query = query.strip()
        if len(query) > MAX_QUERY_CHARS:
            raise AdapterContractError("query exceeds its bound")
        query_image_path, query_image_sha256 = _resolve_image(
            query_image, "query_image", self.allowed_image_root
        )
        diagnostic = _QueryDiagnostic(
            query_sha256=_sha256_text(query),
            query_image_sha256=query_image_sha256,
        )
        self._last_diagnostic = diagnostic
        if not self.available:
            diagnostic.status = "unavailable"
            diagnostic.reason = "backend_unavailable"
            return []
        query_terms = {term.lower() for term in _TOKEN_RE.findall(query)}
        image_matches: set[tuple[str, int, str]] = set()
        if query_image_path is not None:
            for key, record in self._records.items():
                if record.image_path is not None and record.image_path == query_image_path:
                    image_matches.add(key)
        candidates: list[tuple[tuple[str, int, str], int, bool]] = []
        for key, record in self._records.items():
            if record.scope != self.scope and record.scope != "global":
                diagnostic.excluded_scope += 1
                continue
            if record.superseded_by is not None:
                diagnostic.excluded_superseded += 1
                continue
            if record.lifecycle not in _ACTIVE_LIFECYCLES:
                diagnostic.excluded_lifecycle += 1
                continue
            if record.image_path is not None and record.image_sha256 is None:
                diagnostic.missing_images += 1
            record_terms = {term.lower() for term in _TOKEN_RE.findall(record.content)}
            overlap = len(query_terms & record_terms)
            image_match = key in image_matches
            if overlap == 0 and not image_match:
                continue
            candidates.append((key, overlap, image_match))
        # Group by trajectory so event order is never changed by relevance ties
        # or by a later event having one extra matching token.
        trajectory_best: dict[str, int] = {}
        for key, overlap, image_match in candidates:
            trajectory_best[key[0]] = max(trajectory_best.get(key[0], 0), overlap + int(image_match))
        candidates.sort(
            key=lambda row: (
                -trajectory_best[row[0][0]],
                row[0][0],
                row[0][1],
                row[0][2],
            )
        )
        ordered_records: list[_EvidenceRecord] = []
        seen_trajectories: set[str] = set()
        for key, _overlap, _image_match in candidates:
            if key[0] in seen_trajectories:
                continue
            seen_trajectories.add(key[0])
            members = sorted(
                (self._records[item_key] for item_key, _score, _image_match in candidates if item_key[0] == key[0]),
                key=lambda record: (record.event_index, record.event_id),
            )
            ordered_records.extend(members)
        output: list[dict[str, str]] = []
        text_chars = 0
        image_count = 0
        for record in ordered_records:
            if len([item for item in output if item["type"] == "text"]) >= self.max_results:
                break
            text = self._format_record(record)
            remaining = self.max_total_text_chars - text_chars
            if remaining <= 0:
                diagnostic.bounded = False
                break
            text = _one_line(text, min(self.max_text_chars, remaining))
            output.append({"type": "text", "value": text})
            text_chars += len(text)
            if record.conflict_ids:
                diagnostic.conflicts_visible += 1
            if record.image_path is not None and record.image_sha256 is not None and image_count < self.max_image_items:
                output.append({"type": "image", "value": str(record.image_path)})
                image_count += 1
        diagnostic.text_items = sum(item["type"] == "text" for item in output)
        diagnostic.image_items = sum(item["type"] == "image" for item in output)
        diagnostic.bounded = diagnostic.bounded and text_chars <= self.max_total_text_chars
        if output:
            diagnostic.status = "complete"
            diagnostic.reason = "evidence_served"
        else:
            diagnostic.status = "abstained"
            diagnostic.reason = "no_visible_evidence" if diagnostic.excluded_lifecycle or diagnostic.excluded_superseded else "no_evidence"
        return output

    def _format_record(self, record: _EvidenceRecord) -> str:
        return "; ".join(
            [
                f"trajectory_id={record.trajectory_id}",
                f"session_id={record.session_id}",
                f"event_id={record.event_id}",
                f"event_index={record.event_index}",
                f"state_index={record.state_index if record.state_index is not None else 'unknown'}",
                f"timestamp={record.timestamp}",
                f"source_refs={'|'.join(record.source_refs) if record.source_refs else 'missing'}",
                f"scope={record.scope}",
                f"lifecycle={record.lifecycle}",
                f"conflict_ids={'|'.join(record.conflict_ids) if record.conflict_ids else 'none'}",
                f"superseded_by={record.superseded_by or 'none'}",
                f"supersedes={'|'.join(record.supersedes) if record.supersedes else 'none'}",
                f"content={record.content or 'multimodal evidence'}",
            ]
        )

    def diagnostic(self) -> dict[str, Any]:
        return self._last_diagnostic.public()

    def debug_public_state(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for record in sorted(
            self._records.values(),
            key=lambda item: (item.trajectory_id, item.event_index, item.event_id),
        ):
            records.append(
                {
                    "trajectory_id": record.trajectory_id,
                    "session_id": record.session_id,
                    "event_id": record.event_id,
                    "event_index": record.event_index,
                    "state_index": record.state_index,
                    "timestamp": record.timestamp,
                    "source_refs": list(record.source_refs),
                    "scope": record.scope,
                    "lifecycle": record.lifecycle,
                    "conflict_ids": list(record.conflict_ids),
                    "superseded_by": record.superseded_by,
                    "supersedes": list(record.supersedes),
                    "content_sha256": record.content_sha256,
                    "image_sha256": record.image_sha256,
                }
            )
        return {
            "schema_version": ADAPTER_SCHEMA_VERSION,
            "scope": self.scope,
            "available": self.available,
            "trajectory_count": len(self._trajectory_ids),
            "records": records,
            "diagnostic": self.diagnostic(),
        }

    @property
    def memory_config(self) -> dict[str, Any]:
        return {
            "memory_type": ADAPTER_MEMORY_TYPE,
            "memory_params": {
                "scope": self.scope,
                "available": self.available,
                "max_results": self.max_results,
                "max_text_chars": self.max_text_chars,
                "max_total_text_chars": self.max_total_text_chars,
                "max_image_items": self.max_image_items,
            },
        }


def _positive_int(value: Any, field: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > maximum:
        raise AdapterContractError(f"{field} must be an integer in 1..{maximum}")
    return value


class LongMemEvalV2VaultMemory:
    """V2-compatible adapter with the exact ``insert``/``query`` boundary.

    ``insert`` receives only one full trajectory. ``query`` receives only the
    question text and optional image path.  Benchmark identifiers, labels,
    gold answers, and evaluator configuration are intentionally absent from
    both method signatures and the stored projection.
    """

    memory_type = ADAPTER_MEMORY_TYPE

    def __init__(self, memory_params: Mapping[str, Any] | None = None) -> None:
        self._backend = _VaultMemoryBackend(memory_params or {})

    @property
    def memory_config(self) -> dict[str, Any]:
        return self._backend.memory_config

    def insert(self, trajectory: Mapping[str, Any]) -> None:
        self._backend.insert(trajectory)

    def query(self, query: str, query_image: str | None = None) -> list[dict[str, str]]:
        return self._backend.query(query, query_image)

    def post_query_hook(
        self,
        *,
        query: str,
        query_image: str | None,
        memory_context: list[dict[str, str]],
    ) -> dict[str, Any]:
        del query, query_image, memory_context
        return self._backend.diagnostic()

    def debug_public_state(self) -> dict[str, Any]:
        return self._backend.debug_public_state()
