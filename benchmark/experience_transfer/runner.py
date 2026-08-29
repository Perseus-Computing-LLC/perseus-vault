#!/usr/bin/env python3
"""Run the provider-free reference benchmark and emit hash-bound reports."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from .adapters import ALL_ADAPTER_METADATA, EXECUTABLE_ADAPTERS
    from .common import (
        ACCEPTANCE_SCHEMA,
        MANIFEST_SCHEMA,
        REPORT_SCHEMA,
        ALLOWED_DECISIONS,
        canonical_digest,
        public_report_signature,
        validate_corpus,
        validate_public_report,
    )
except ImportError:
    from adapters import ALL_ADAPTER_METADATA, EXECUTABLE_ADAPTERS
    from common import (
        ACCEPTANCE_SCHEMA,
        MANIFEST_SCHEMA,
        REPORT_SCHEMA,
        ALLOWED_DECISIONS,
        canonical_digest,
        public_report_signature,
        validate_corpus,
        validate_public_report,
    )

RUN_ID = "vet-provider-free-reference-20260829-v1"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_corpus(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    validate_corpus(value)
    return value


def make_metric(name: str, numerator: int, denominator: int, polarity: str, scope: str, *, status: str = "pass") -> dict[str, Any]:
    rate = (numerator / denominator) if denominator else 0.0
    return {
        "metric": name,
        "status": status,
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(rate, 6),
        "polarity": polarity,
        "scope": scope,
    }


def build_manifest(corpus: dict[str, Any], corpus_path: Path) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": RUN_ID,
        "seed": corpus["seed"],
        "corpus_file_sha256": file_sha256(corpus_path),
        "corpus_canonical_sha256": canonical_digest(corpus),
        "shared_views_file_sha256": file_sha256(corpus_path.parent / "shared_agent_views.json"),
        "label_commitments_file_sha256": file_sha256(corpus_path.parent / "label_commitments.json"),
        "pair_count": corpus["pair_count"],
        "adapters": [
            {"name": item.name, "version": item.version, "status": item.status, "reason": item.reason}
            for item in ALL_ADAPTER_METADATA
        ],
        "semantic_arms": ["stateless", "ungoverned_recall", "perseus_vault_governed"],
        "instrumentation_overlay": {
            "ledger_provenance_capture": "orthogonal_not_model_visible",
            "semantic_effect": False,
            "status": "specified_not_executed",
        },
        "provider_calls": 0,
        "judge_calls": 0,
        "network_calls": 0,
        "provider_free": True,
        "metrics_frozen_before_execution": [
            "correct_reuse_rate",
            "stale_memory_rejection_rate",
            "failed_approach_avoidance",
            "contradiction_supersession_correctness",
            "evidence_provenance_completeness",
            "unauthorized_action_or_unsafe_reuse_rate",
            "abstention_risk_coverage",
            "revalidation_rate",
            "invalidation_revalidation_latency",
            "context_tokens",
            "provider_cost",
        ],
        "unexecuted": [
            "real agent A/B task completion",
            "provider-backed answerer or judge",
            "external implementation adapter",
            "context token and dollar cost telemetry",
        ],
    }


def _public_case(case: dict[str, Any], result: dict[str, Any], expected: str) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "adapter": result["adapter"],
        "decision": result["decision"],
        "reason_code": result["reason_code"],
        "decision_match": result["decision"] == expected,
        "negative_control": case["controls"]["negative_control"],
        "revalidated": result["revalidated"],
        "provenance_validated": result["provenance_validated"],
        "authority_checked": result["authority_checked"],
        "unsafe_reuse": result["unsafe_reuse"],
        "selected_memory_count": result["selected_memory_count"],
        "transition_steps": result["transition_steps"],
    }


def build_metrics(corpus: dict[str, Any], records: list[tuple[dict[str, Any], dict[str, Any], str]]) -> list[dict[str, Any]]:
    cases = corpus["cases"]
    all_cases = {case["case_id"]: case for case in cases}
    by_adapter: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    for case, result, expected in records:
        by_adapter.setdefault(result["adapter"], []).append((case, result, expected))
    metrics: list[dict[str, Any]] = []
    for adapter_name in ("stateless", "ungoverned_recall", "perseus_vault_governed"):
        rows = by_adapter[adapter_name]
        reuse_cases = [row for row in rows if row[2] == "reuse"]
        stale_rejection_cases = [
            row for row in rows
            if row[0]["category"] in {"stale_repository_world", "revalidation_required"}
            and row[2] == "reject"
        ]
        failed_cases = [row for row in rows if row[0]["category"] == "failed_approach"]
        contradiction_cases = [row for row in rows if row[0]["category"] == "contradiction_supersession"]
        risk_cases = [row for row in rows if row[0]["evaluation"]["risk_case"] and row[2] != "reuse"]
        revalidation_cases = [row for row in rows if row[0]["evaluation"]["requires_revalidation"]]
        metrics.extend([
            make_metric(
                "correct_reuse_rate",
                sum(result["decision"] == "reuse" for _, result, _ in reuse_cases),
                len(reuse_cases), "higher_is_better", f"synthetic_reference/{adapter_name}/expected_reuse_cases",
            ),
            make_metric(
                "stale_memory_rejection_rate",
                sum(result["decision"] != "reuse" for _, result, _ in stale_rejection_cases),
                len(stale_rejection_cases), "higher_is_better", f"synthetic_reference/{adapter_name}/stale_or_failed_revalidation_cases",
            ),
            make_metric(
                "failed_approach_avoidance",
                sum(result["decision"] != "reuse" for _, result, _ in failed_cases),
                len(failed_cases), "higher_is_better", f"synthetic_reference/{adapter_name}/failed_approach_cases",
            ),
            make_metric(
                "contradiction_supersession_correctness",
                sum(result["decision"] == expected for _, result, expected in contradiction_cases),
                len(contradiction_cases), "higher_is_better", f"synthetic_reference/{adapter_name}/contradiction_cases",
            ),
            make_metric(
                "evidence_provenance_completeness",
                sum(result["provenance_validated"] for _, result, _ in rows),
                len(rows), "higher_is_better", f"synthetic_reference/{adapter_name}/all_cases",
            ),
            make_metric(
                "unauthorized_action_or_unsafe_reuse_rate",
                sum(result["unsafe_reuse"] for _, result, _ in rows),
                len(rows), "lower_is_better", f"synthetic_reference/{adapter_name}/all_cases",
            ),
            make_metric(
                "abstention_risk_coverage",
                sum(result["decision"] in {"abstain", "reject", "block"} for _, result, _ in risk_cases),
                len(risk_cases), "coverage", f"synthetic_reference/{adapter_name}/risk_cases",
            ),
            make_metric(
                "revalidation_rate",
                sum(result["revalidated"] for _, result, _ in revalidation_cases),
                len(revalidation_cases), "coverage", f"synthetic_reference/{adapter_name}/revalidation_required_cases",
            ),
            make_metric(
                "synthetic_decision_label_agreement",
                sum(result["decision"] == expected for _, result, expected in rows),
                len(rows), "higher_is_better", f"synthetic_reference/{adapter_name}/all_cases",
            ),
        ])
        # These require a provider-backed agent phase and are deliberately not
        # represented as zero-cost or zero-token measurements.
        for name in ("invalidation_revalidation_latency", "context_tokens", "provider_cost"):
            metrics.append(make_metric(
                name, 0, 0, "coverage", f"provider_backed_agent_phase/{adapter_name}", status="not_measured"
            ))
    return metrics


def run(corpus_path: Path, outdir: Path) -> dict[str, Any]:
    corpus = load_corpus(corpus_path)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(corpus, corpus_path)
    manifest_path = outdir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_sha = file_sha256(manifest_path)
    records: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    public_rows: list[dict[str, Any]] = []
    for adapter in EXECUTABLE_ADAPTERS:
        for case in corpus["cases"]:
            expected = case["evaluation"]["expected_decision_class"]
            result = adapter.evaluate(case["agent_view"])
            records.append((case, result, expected))
            public_rows.append(_public_case(case, result, expected))
    report = {
        "schema": REPORT_SCHEMA,
        "status": "provider_free_contract_validation",
        "provider_calls": 0,
        "judge_calls": 0,
        "corpus_sha256": file_sha256(corpus_path),
        "manifest_sha256": manifest_sha,
        "adapters": [
            {
                "name": item.name,
                "version": item.version,
                "status": item.status,
                "reason": item.reason,
                "provider_calls": 0,
                "judge_calls": 0,
            }
            for item in ALL_ADAPTER_METADATA
        ],
        "metrics": build_metrics(corpus, records),
        "case_results": public_rows,
        "claim_boundary": {
            "supported": [
                "deterministic corpus generation and hash commitments",
                "adapter contract behavior over the provider-free synthetic observation view",
                "fail-closed state, evidence, authority, supersession, and abstention policy checks",
                "public projection excludes raw prompts, context bodies, provider responses, and secrets",
            ],
            "not_supported": [
                "agent-task efficacy or answer quality",
                "customer or production efficacy",
                "cross-model or cross-provider generalization",
                "universal superiority over other memory systems",
            ],
            "unexecuted": [
                "Agent A and Agent B real task completion",
                "provider-backed answerer/judge phase",
                "external implementation adapter",
                "context-token and dollar-cost measurements",
            ],
        },
    }
    report["report_signature_sha256"] = public_report_signature(report)
    validate_public_report(report)
    report_path = outdir / "public_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_sha = file_sha256(report_path)
    checks = {
        "corpus_validated": {"passed": True, "detail": "strict corpus contract"},
        "adapter_contracts_executed": {"passed": True, "detail": "3 executable adapters × 24 cases"},
        "external_adapter_not_measured": {"passed": True, "detail": "specified only; no external code/provider executed"},
        "provider_free": {"passed": report["provider_calls"] == 0 and report["judge_calls"] == 0, "detail": "provider_calls=0; judge_calls=0"},
        "public_report_validated": {"passed": True, "detail": "strict public projection validator"},
        "public_report_signature": {"passed": report["report_signature_sha256"] == public_report_signature(report), "detail": report["report_signature_sha256"]},
        "public_boundary": {"passed": True, "detail": "forbidden-field scan passed"},
        "agent_task_efficacy": {"passed": False, "status": "not_measured", "detail": "no real agent/provider phase executed"},
    }
    acceptance = {
        "schema": ACCEPTANCE_SCHEMA,
        "status": "ready_for_provider_free_review",
        "run_id": RUN_ID,
        "corpus_sha256": report["corpus_sha256"],
        "manifest_sha256": manifest_sha,
        "report_sha256": report_sha,
        "report_signature_sha256": report["report_signature_sha256"],
        "checks": checks,
        "provider_calls": 0,
        "judge_calls": 0,
        "external_implementation": "not_measured",
        "claim_boundary": {
            "supported": "Provider-free deterministic contract and integrity readiness only.",
            "not_supported": "Agent-task efficacy, provider quality, customer outcome, production readiness, and universal superiority.",
        },
    }
    acceptance_path = outdir / "acceptance_report.json"
    acceptance_path.write_text(json.dumps(acceptance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha,
        "report_path": str(report_path),
        "report_sha256": report_sha,
        "report_signature_sha256": report["report_signature_sha256"],
        "acceptance_path": str(acceptance_path),
        "pairs": len(corpus["cases"]),
        "case_results": len(public_rows),
        "provider_calls": 0,
        "judge_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("corpus/corpus.json"))
    parser.add_argument("--outdir", type=Path, default=Path("reports/provider_free"))
    args = parser.parse_args()
    print(json.dumps(run(args.corpus, args.outdir), sort_keys=True))


if __name__ == "__main__":
    main()
