from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    memory_key: str
    scope: str
    text: str
    source_ref: str
    record_digest: str
    actor: str
    trust: str
    valid_from: int
    recorded_at: int
    status: str = "active"
    supersedes: tuple[str, ...] = ()
    valid_to: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MemoryRecord":
        return cls(
            record_id=str(value["record_id"]),
            memory_key=str(value["memory_key"]),
            scope=str(value["scope"]),
            text=str(value["text"]),
            source_ref=str(value["source_ref"]),
            record_digest=str(value["record_digest"]),
            actor=str(value["actor"]),
            trust=str(value["trust"]),
            valid_from=int(value["valid_from"]),
            recorded_at=int(value["recorded_at"]),
            status=str(value.get("status", "active")),
            supersedes=tuple(str(item) for item in value.get("supersedes", ())),
            valid_to=(int(value["valid_to"]) if value.get("valid_to") is not None else None),
        )

    def with_status(self, status: str, *, valid_to: int | None = None) -> "MemoryRecord":
        return replace(self, status=status, valid_to=valid_to if valid_to is not None else self.valid_to)

    def to_hit(self, score: float = 0.0) -> "MemoryHit":
        return MemoryHit(
            record_id=self.record_id,
            memory_key=self.memory_key,
            scope=self.scope,
            text=self.text,
            source_ref=self.source_ref,
            record_digest=self.record_digest,
            actor=self.actor,
            trust=self.trust,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            status=self.status,
            score=score,
        )


@dataclass(frozen=True)
class MemoryHit:
    record_id: str
    memory_key: str
    scope: str
    text: str
    source_ref: str
    record_digest: str
    actor: str
    trust: str
    valid_from: int
    valid_to: int | None
    status: str
    score: float = 0.0


@dataclass(frozen=True)
class AdmissionReceipt:
    record_id: str
    status: str
    serveable: bool
    reason_codes: tuple[str, ...] = ()
    record_digest: str = ""


@dataclass(frozen=True)
class MutationReceipt:
    record_id: str
    status: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalResult:
    decision: str
    hits: tuple[MemoryHit, ...] = ()
    reason_codes: tuple[str, ...] = ()
    error: str | None = None

    @classmethod
    def blocked(cls, reason: str) -> "RetrievalResult":
        return cls(decision="blocked", reason_codes=(reason,), error=reason)

    @classmethod
    def failed(cls, reason: str) -> "RetrievalResult":
        return cls(decision="error", reason_codes=(reason,), error=reason)
