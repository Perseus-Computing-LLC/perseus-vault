from __future__ import annotations

import re
from dataclasses import replace
from difflib import SequenceMatcher

from .models import AdmissionReceipt, MemoryRecord, MutationReceipt, RetrievalResult
from .provider import HostileMemoryProvider

_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?prior\s+instructions|reveal\s+(?:the\s+)?credentials|"
    r"system\s+prompt|jailbreak|developer\s+message",
    re.IGNORECASE,
)


def _tokens(value: str) -> set[str]:
    return set(_WORD_RE.findall(value.casefold()))


def _similarity(left: str, right: str) -> float:
    left_words = _tokens(left)
    right_words = _tokens(right)
    if not left_words or not right_words:
        return 0.0
    jaccard = len(left_words & right_words) / len(left_words | right_words)
    sequence = SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
    return 0.65 * jaccard + 0.35 * sequence


def _query_score(query: str, record: MemoryRecord) -> float:
    q = _tokens(query)
    searchable = _tokens(f"{record.memory_key} {record.text}")
    if not q or not searchable:
        return 0.0
    overlap = len(q & searchable)
    if not overlap:
        return 0.0
    return overlap / len(q)


class ReferenceProvider(HostileMemoryProvider):
    """Small deterministic policy-aware provider used as a control arm.

    It is intentionally boring: no embeddings, no LLM, no network. Its value is
    that the benchmark can prove its own contract and compare an unsafe baseline
    without making a model call part of the harness.
    """

    name = "reference-policy-provider"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.tombstones: set[str] = set()
        self.quarantined: dict[str, AdmissionReceipt] = {}

    def ingest(self, record: MemoryRecord) -> AdmissionReceipt:
        if record.record_id in self.records or record.record_id in self.quarantined:
            receipt = AdmissionReceipt(
                record.record_id, "quarantined", False, ("duplicate_replay",), record.record_digest
            )
            self.quarantined[record.record_id] = receipt
            return receipt
        if any(
            existing.scope == record.scope
            and existing.record_digest == record.record_digest
            for existing in self.records.values()
        ):
            receipt = AdmissionReceipt(
                record.record_id, "quarantined", False, ("duplicate_content",), record.record_digest
            )
            self.quarantined[record.record_id] = receipt
            return receipt
        if _INJECTION_RE.search(record.text):
            receipt = AdmissionReceipt(
                record.record_id, "quarantined", False, ("prompt_injection_text",), record.record_digest
            )
            self.quarantined[record.record_id] = receipt
            return receipt
        same_time_conflict = [
            existing for existing in self.records.values()
            if existing.scope == record.scope
            and existing.memory_key == record.memory_key
            and existing.valid_from == record.valid_from
            and existing.trust == "authoritative"
            and record.trust == "authoritative"
            and existing.text != record.text
            and existing.status not in {"archived", "quarantined"}
        ]
        if same_time_conflict:
            receipt = AdmissionReceipt(
                record.record_id, "quarantined", False,
                ("same_time_conflict",), record.record_digest
            )
            self.quarantined[record.record_id] = receipt
            return receipt
        if record.trust in {"untrusted", "unknown"}:
            authoritative = [
                existing for existing in self.records.values()
                if existing.scope == record.scope
                and existing.memory_key == record.memory_key
                and existing.trust == "authoritative"
                and existing.status not in {"archived", "quarantined"}
            ]
            if authoritative and any(existing.text != record.text for existing in authoritative):
                receipt = AdmissionReceipt(
                    record.record_id, "quarantined", False,
                    ("low_trust_conflict",), record.record_digest
                )
                self.quarantined[record.record_id] = receipt
                return receipt
        near_duplicate = [
            existing for existing in self.records.values()
            if existing.scope == record.scope and existing.memory_key == record.memory_key
            and existing.status not in {"archived", "quarantined"}
            and _similarity(existing.text, record.text) >= 0.96
        ]
        if near_duplicate:
            receipt = AdmissionReceipt(
                record.record_id, "quarantined", False,
                ("near_duplicate_flood",), record.record_digest
            )
            self.quarantined[record.record_id] = receipt
            return receipt

        for old_id in record.supersedes:
            old = self.records.get(old_id)
            if old is not None and old.scope == record.scope:
                self.records[old_id] = replace(old, status="superseded", valid_to=record.valid_from)
        # A newer version may arrive before its older source record. Preserve
        # that late source as history with the future superseder's boundary.
        future_superseders = [
            existing for existing in self.records.values()
            if record.record_id in existing.supersedes
            and existing.scope == record.scope
            and existing.valid_from > record.valid_from
        ]
        if future_superseders:
            boundary = min(existing.valid_from for existing in future_superseders)
            record = replace(record, status="superseded", valid_to=boundary)
        self.records[record.record_id] = record
        return AdmissionReceipt(record.record_id, "admitted", True, (), record.record_digest)

    def forget(self, scope: str, record_id: str) -> MutationReceipt:
        record = self.records.get(record_id)
        if record is None or record.scope != scope:
            return MutationReceipt(record_id, "not_found", ("scope_or_record_not_found",))
        self.records[record_id] = replace(record, status="archived")
        self.tombstones.add(record_id)
        return MutationReceipt(record_id, "archived", ("tombstoned",))

    def retrieve(self, query: str, scope: str, as_of: int, limit: int) -> RetrievalResult:
        if not scope.strip():
            return RetrievalResult.blocked("scope_required")
        candidates: list[tuple[float, MemoryRecord]] = []
        for record in self.records.values():
            if record.scope != scope or record.status in {"archived", "quarantined"}:
                continue
            if record.valid_from > as_of:
                continue
            if record.valid_to is not None and as_of >= record.valid_to:
                continue
            if record.trust in {"untrusted", "unknown"}:
                continue
            score = _query_score(query, record)
            if score > 0:
                candidates.append((score, record))
        candidates.sort(key=lambda pair: (-pair[0], -pair[1].valid_from, pair[1].record_id))
        hits = tuple(record.to_hit(score=score) for score, record in candidates[:limit])
        if not hits:
            return RetrievalResult("abstain", (), ("no_trustworthy_evidence",))
        return RetrievalResult("answer", hits, ())


class NaiveProvider(HostileMemoryProvider):
    """Deliberately unsafe negative control: global, stale, and fail-open."""

    name = "naive-global-provider"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.tombstones: set[str] = set()

    def ingest(self, record: MemoryRecord) -> AdmissionReceipt:
        self.records[record.record_id] = record
        return AdmissionReceipt(record.record_id, "admitted", True, (), record.record_digest)

    def forget(self, scope: str, record_id: str) -> MutationReceipt:
        self.tombstones.add(record_id)
        return MutationReceipt(record_id, "archived", ("unsafe_global_tombstone",))

    def retrieve(self, query: str, scope: str, as_of: int, limit: int) -> RetrievalResult:
        candidates = []
        for record in self.records.values():
            score = _query_score(query, record)
            if score > 0:
                candidates.append((score, record))
        candidates.sort(key=lambda pair: (-pair[0], pair[1].record_id))
        hits = tuple(record.to_hit(score=score) for score, record in candidates[:limit])
        return RetrievalResult("answer" if hits else "abstain", hits, ())


class NoMemoryProvider(HostileMemoryProvider):
    """Negative control that always abstains."""

    name = "no-memory-provider"

    def reset(self) -> None:
        return None

    def ingest(self, record: MemoryRecord) -> AdmissionReceipt:
        return AdmissionReceipt(record.record_id, "admitted", True, ("discarded_control",), record.record_digest)

    def forget(self, scope: str, record_id: str) -> MutationReceipt:
        return MutationReceipt(record_id, "not_found", ("no_memory_control",))

    def retrieve(self, query: str, scope: str, as_of: int, limit: int) -> RetrievalResult:
        return RetrievalResult("abstain", (), ("no_memory_control",))
