"""Deterministic provenance-preserving context-selection benchmark (#820).

The harness evaluates context selection only. It never calls a model or judge,
so retrieval, context-selection, and end-to-end QA metrics cannot be confused.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "perseus-vault-context-selection/v1"
VARIANTS = (
    "full_retrieved",
    "hybrid_ranked",
    "provenance_filtered",
    "compact_evidence_linked",
)
POSITIONS = ("front", "middle", "tail")
RELEVANCE_STOPWORDS = {
    "a",
    "after",
    "are",
    "be",
    "current",
    "for",
    "is",
    "of",
    "the",
    "this",
    "to",
    "what",
}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def relevance_terms(value: str) -> set[str]:
    return tokens(value) - RELEVANCE_STOPWORDS


@dataclass(frozen=True)
class ContextItem:
    entity_id: str
    source_ids: tuple[str, ...]
    body: str
    workspace_hash: str
    valid_from_unix_ms: int
    valid_to_unix_ms: int | None
    certainty: float
    provenance_class: str | None
    authoritative: bool
    superseded: bool
    position: int

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "ContextItem":
        return cls(
            entity_id=str(raw["entity_id"]),
            source_ids=tuple(str(item) for item in raw.get("source_ids", [])),
            body=str(raw["body"]),
            workspace_hash=str(raw["workspace_hash"]),
            valid_from_unix_ms=int(raw["valid_from_unix_ms"]),
            valid_to_unix_ms=(
                None
                if raw.get("valid_to_unix_ms") is None
                else int(raw["valid_to_unix_ms"])
            ),
            certainty=float(raw.get("certainty", 0.0)),
            provenance_class=raw.get("provenance_class"),
            authoritative=bool(raw.get("authoritative", False)),
            superseded=bool(raw.get("superseded", False)),
            position=int(raw.get("position", 0)),
        )

    def valid_at(self, timestamp: int) -> bool:
        return self.valid_from_unix_ms <= timestamp and (
            self.valid_to_unix_ms is None or timestamp < self.valid_to_unix_ms
        )

    def evidence_linked(self) -> bool:
        return bool(self.source_ids) and self.provenance_class in {
            "source_human",
            "fact_extracted",
            "fact_derived",
        }

    def as_public_metadata(self) -> dict[str, Any]:
        """Return metadata safe for a report; body text is deliberately omitted."""
        return {
            "entity_id": self.entity_id,
            "source_ids": list(self.source_ids),
            "workspace_hash": self.workspace_hash,
            "valid_from_unix_ms": self.valid_from_unix_ms,
            "valid_to_unix_ms": self.valid_to_unix_ms,
            "certainty": self.certainty,
            "provenance_class": self.provenance_class,
            "authoritative": self.authoritative,
            "superseded": self.superseded,
            "position": self.position,
        }


@dataclass(frozen=True)
class Question:
    question_id: str
    query: str
    workspace_hash: str
    valid_at_unix_ms: int
    expected_source_ids: frozenset[str]
    positions: tuple[str, ...]

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Question":
        positions = tuple(raw.get("positions", POSITIONS))
        if not positions or any(position not in POSITIONS for position in positions):
            raise ValueError(f"invalid prompt positions for {raw.get('question_id')}")
        return cls(
            question_id=str(raw["question_id"]),
            query=str(raw["query"]),
            workspace_hash=str(raw["workspace_hash"]),
            valid_at_unix_ms=int(raw["valid_at_unix_ms"]),
            expected_source_ids=frozenset(str(item) for item in raw["expected_source_ids"]),
            positions=positions,
        )


def _lexical_score(question: Question, item: ContextItem) -> tuple[int, float, int, str]:
    overlap = len(relevance_terms(question.query) & relevance_terms(item.body))
    return (overlap, item.certainty, -item.position, item.entity_id)


def select_context(variant: str, question: Question, items: Iterable[ContextItem]) -> list[ContextItem]:
    """Select a deterministic candidate list without a model or attention claim."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown selection variant: {variant}")
    candidates = list(items)
    if variant == "full_retrieved":
        return sorted(candidates, key=lambda item: (item.position, item.entity_id))
    ranked = sorted(candidates, key=lambda item: _lexical_score(question, item), reverse=True)
    if variant == "hybrid_ranked":
        return ranked
    filtered = [
        item
        for item in ranked
        if item.workspace_hash == question.workspace_hash
        and len(relevance_terms(question.query) & relevance_terms(item.body)) >= 2
        and item.valid_at(question.valid_at_unix_ms)
        and not item.superseded
        and item.authoritative
    ]
    if variant == "provenance_filtered":
        return filtered
    # Compact mode keeps at most one entity per source group while retaining the
    # evidence-linked, authoritative candidates. This is a context-shape metric,
    # not an answer-quality or model-quality claim.
    selected: list[ContextItem] = []
    seen_sources: set[str] = set()
    for item in filtered:
        source_key = item.source_ids[0] if item.source_ids else item.entity_id
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        if item.evidence_linked():
            selected.append(item)
    return selected


def row_for(question: Question, position: str, variant: str, selected: list[ContextItem]) -> dict[str, Any]:
    selected_sources = sorted({source for item in selected for source in item.source_ids})
    expected = set(question.expected_source_ids)
    chosen = set(selected_sources)
    true_positive = len(expected & chosen)
    precision = true_positive / len(chosen) if chosen else 0.0
    recall = true_positive / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    unsupported = sum(
        1
        for item in selected
        if item.workspace_hash != question.workspace_hash
        or not item.valid_at(question.valid_at_unix_ms)
        or item.superseded
        or not item.authoritative
    )
    return {
        "question_id": question.question_id,
        "query_sha256": sha256_text(question.query),
        "prompt_position": position,
        "variant": variant,
        "workspace_hash": question.workspace_hash,
        "valid_at_unix_ms": question.valid_at_unix_ms,
        "expected_source_ids": sorted(expected),
        "selected_source_ids": selected_sources,
        "selected_entity_ids": [item.entity_id for item in selected],
        "selected_metadata": [item.as_public_metadata() for item in selected],
        "selected_token_count": sum(len(tokens(item.body)) for item in selected),
        "metrics": {
            "evidence_precision": round(precision, 6),
            "evidence_recall": round(recall, 6),
            "evidence_f1": round(f1, 6),
            "unsupported_selection_rate": round(unsupported / len(selected), 6) if selected else 0.0,
        },
        "abstained": not bool(selected),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"row_count": 0, "status": "no_rows"}
    metrics = [row["metrics"] for row in rows]
    return {
        "row_count": len(rows),
        "evidence_precision": round(sum(item["evidence_precision"] for item in metrics) / len(metrics), 6),
        "evidence_recall": round(sum(item["evidence_recall"] for item in metrics) / len(metrics), 6),
        "evidence_f1": round(sum(item["evidence_f1"] for item in metrics) / len(metrics), 6),
        "unsupported_selection_rate": round(
            sum(item["unsupported_selection_rate"] for item in metrics) / len(metrics), 6
        ),
        "mean_selected_token_count": round(
            sum(row["selected_token_count"] for row in rows) / len(rows), 3
        ),
    }


def run_benchmark(dataset: dict[str, Any], *, model_id: str = "not-run", judge_id: str = "not-run") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if dataset.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported dataset schema_version")
    items = [ContextItem.from_json(item) for item in dataset.get("items", [])]
    questions = [Question.from_json(item) for item in dataset.get("questions", [])]
    if not items or not questions:
        raise ValueError("dataset must contain items and questions")
    rows: list[dict[str, Any]] = []
    for question in questions:
        for position in question.positions:
            for variant in VARIANTS:
                rows.append(row_for(question, position, variant, select_context(variant, question, items)))
    by_variant = {
        variant: _aggregate([row for row in rows if row["variant"] == variant]) for variant in VARIANTS
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "provenance-preserving-context-selection",
        "methodology": {
            "selection_only": True,
            "tokenizer": "whitespace-regex-v1",
            "attention_weights_used_as_evidence": False,
            "prompt_position_permutations": list(POSITIONS),
            "model_id": model_id,
            "judge_id": judge_id,
        },
        "corpus": {
            "corpus_id": dataset.get("corpus_id", "unspecified"),
            "split": dataset.get("split", "unspecified"),
            "item_count": len(items),
            "question_count": len(questions),
            "top_k": len(items),
        },
        "metrics": {
            "retrieval_only": {"status": "not_run", "reason": "candidate set is supplied by the fixture"},
            "context_selection": by_variant,
            "end_to_end_qa": {"status": "not_run", "reason": "no model or judge call is made by this harness"},
        },
        "negative_controls": {
            "attention_weights_not_evidence": True,
            "unsupported_inference_from_attention_weights": "not measured; no attention data accepted",
        },
        "raw_rows": "raw_rows.jsonl",
        "raw_rows_sha256": sha256_json(rows),
        "signature": {"algorithm": "sha256-canonical-json-v1", "value": None},
    }
    unsigned = dict(report)
    unsigned["signature"] = dict(report["signature"])
    report["signature"]["value"] = sha256_json(unsigned)
    return report, rows


def verify_report(report: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    if report.get("raw_rows_sha256") != sha256_json(rows):
        return False
    signature = report.get("signature", {})
    expected = signature.get("value")
    unsigned = dict(report)
    unsigned["signature"] = {"algorithm": signature.get("algorithm"), "value": None}
    return bool(expected) and expected == sha256_json(unsigned)


def write_outputs(output_dir: str | Path, report: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    raw = output / "raw_rows.jsonl"
    raw.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))
    (output / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
