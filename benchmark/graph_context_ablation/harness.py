#!/usr/bin/env python3
"""Provider-free matched graph-context ablation for Perseus Vault issue #1143."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import deque
from pathlib import Path
from typing import Any

FIXTURE_SCHEMA = "perseus-vault-graph-context-ablation-fixture/v1"
REPORT_SCHEMA = "perseus-vault-graph-context-ablation-report/v1"
GRAPH_REASONS = {"multi_hop", "relational", "entity_centric", "global"}
REQUIRED_SHAPES = {
    "true_multi_hop",
    "single_hop_control",
    "stale_current_conflict",
    "unsupported_declared_edge",
    "cross_scope_target",
    "no_signal_utility_skip",
}
SOURCE_TYPES = {
    "adr",
    "meeting_notes",
    "slack_thread",
    "postmortem",
    "service_manifest",
}
FORBIDDEN_REPORT_KEYS = {
    "api_key",
    "authorization",
    "credential",
    "password",
    "prompt_text",
    "query",
    "raw_body",
    "response_text",
    "secret",
    "token",
}


class ContractError(ValueError):
    """Raised when a fixture or report violates the ablation contract."""


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(stable_json(value))


def _tokens(value: str) -> int:
    return len(re.findall(r"[a-z0-9]+", value.lower()))


def _valid_at(value: dict[str, Any], timestamp: int) -> bool:
    start = int(value["valid_from_unix_ms"])
    end = value.get("valid_to_unix_ms")
    return start <= timestamp and (end is None or timestamp < int(end))


def _normalized_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(fixture)
    for key, identity in (
        ("sources", "source_id"),
        ("nodes", "node_id"),
        ("edges", "edge_id"),
        ("cases", "case_id"),
    ):
        values = normalized.get(key)
        if not isinstance(values, list) or not values:
            raise ContractError(f"fixture {key} must be a non-empty list")
        normalized[key] = sorted(values, key=lambda item: str(item.get(identity, "")))
    return normalized


def _validate_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(fixture, dict) or fixture.get("schema_version") != FIXTURE_SCHEMA:
        raise ContractError("unsupported fixture schema")
    normalized = _normalized_fixture(fixture)
    workspace = normalized.get("workspace_hash")
    valid_at = normalized.get("valid_at_unix_ms")
    config = normalized.get("matched_config")
    if not isinstance(workspace, str) or not workspace or not isinstance(valid_at, int):
        raise ContractError("fixture workspace and valid_at are required")
    if not isinstance(config, dict):
        raise ContractError("matched_config is required")
    required_config = {
        "retrieval_mode",
        "top_k",
        "context_token_budget",
        "reader",
        "prompt",
        "judge",
        "seed",
        "graph_max_hops",
    }
    if set(config) != required_config:
        raise ContractError("matched_config fields do not match the v1 contract")
    if not all(
        isinstance(config[key], str) and config[key]
        for key in ("retrieval_mode", "reader", "prompt", "judge")
    ):
        raise ContractError("matched_config string fields must be non-empty")
    if not all(
        isinstance(config[key], int) and config[key] > 0
        for key in ("top_k", "context_token_budget", "seed", "graph_max_hops")
    ):
        raise ContractError("matched_config numeric fields must be positive integers")

    sources = {item["source_id"]: item for item in normalized["sources"]}
    nodes = {item["node_id"]: item for item in normalized["nodes"]}
    if len(sources) != len(normalized["sources"]) or len(nodes) != len(
        normalized["nodes"]
    ):
        raise ContractError("source and node identifiers must be unique")
    if {item.get("source_type") for item in sources.values()} != SOURCE_TYPES:
        raise ContractError("fixture must contain the five required source types")
    for source in sources.values():
        if not all(
            isinstance(source.get(key), str) and source[key]
            for key in ("source_id", "source_type", "revision", "content")
        ):
            raise ContractError("source fields must be non-empty strings")
    for node in nodes.values():
        if not isinstance(node.get("source_ids"), list) or not node["source_ids"]:
            raise ContractError("every node needs source_ids")
        if any(source_id not in sources for source_id in node["source_ids"]):
            raise ContractError("node references an unknown source")
        if not isinstance(node.get("summary"), str) or not node["summary"]:
            raise ContractError("node summary is required")
        _valid_at(node, int(valid_at))

    edge_ids: set[str] = set()
    for edge in normalized["edges"]:
        edge_id = edge.get("edge_id")
        if not isinstance(edge_id, str) or not edge_id or edge_id in edge_ids:
            raise ContractError("edge identifiers must be unique and non-empty")
        edge_ids.add(edge_id)
        if edge.get("from_node_id") not in nodes or edge.get("to_node_id") not in nodes:
            raise ContractError("edge references an unknown node")
        source = sources.get(edge.get("source_id"))
        if source is None or edge.get("source_revision") != source.get("revision"):
            raise ContractError("edge source revision does not resolve")
        if edge.get("origin") not in {"declared", "derived"}:
            raise ContractError("edge origin is invalid")
        if edge.get("support_state") not in {"supported", "unsupported"}:
            raise ContractError("edge support_state is invalid")
        if edge["support_state"] == "supported" and not edge.get("evidence_anchor"):
            raise ContractError("supported edge requires an evidence anchor")
        _valid_at(edge, int(valid_at))

    shapes = {item.get("shape") for item in normalized["cases"]}
    if shapes != REQUIRED_SHAPES:
        raise ContractError("fixture cases do not cover the required shapes")
    if not any(
        item.get("expected_answer") == "abstain" for item in normalized["cases"]
    ):
        raise ContractError("fixture requires an abstention case")
    for case in normalized["cases"]:
        if not isinstance(case.get("query"), str) or not case["query"]:
            raise ContractError("case query is required")
        if case.get("graph_utility_reason") not in GRAPH_REASONS | {
            "ordinary",
            "temporal",
            "no_signal",
        }:
            raise ContractError("case graph utility reason is invalid")
        if any(node_id not in nodes for node_id in case.get("seed_node_ids", [])):
            raise ContractError("case references an unknown seed node")
        if any(
            source_id not in sources
            for source_id in case.get("required_source_ids", [])
        ):
            raise ContractError("case references an unknown required source")
        if case.get("expected_answer") not in {"correct", "abstain"}:
            raise ContractError("case expected_answer is invalid")
    return normalized


def _source_projection(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source_id,
            "source_type": source["source_type"],
            "revision": source["revision"],
            "content_sha256": sha256_text(source["content"]),
        }
        for source_id, source in sorted(sources.items())
    ]


def _edge_reason(
    edge: dict[str, Any], target: dict[str, Any], workspace: str, timestamp: int
) -> str | None:
    if edge["support_state"] != "supported" or not edge.get("evidence_anchor"):
        return "unsupported_edge"
    if edge["workspace_hash"] not in {workspace, ""} or target[
        "workspace_hash"
    ] not in {workspace, ""}:
        return "cross_scope"
    if not _valid_at(edge, timestamp) or not _valid_at(target, timestamp):
        return "stale_source"
    return None


def _run_case(
    case: dict[str, Any],
    *,
    cell_id: str,
    graph_enabled: bool,
    fixture: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    workspace = fixture["workspace_hash"]
    timestamp = fixture["valid_at_unix_ms"]
    config = fixture["matched_config"]
    selected: list[str] = []
    selected_set: set[str] = set()
    candidate_decisions: list[dict[str, Any]] = []
    edge_decisions: list[dict[str, Any]] = []
    selected_paths: list[dict[str, Any]] = []
    used_tokens = 0

    def consider_node(node_id: str, reason: str) -> bool:
        nonlocal used_tokens
        node = nodes[node_id]
        disposition = None
        if node["workspace_hash"] not in {workspace, ""}:
            disposition = "filtered_scope"
        elif not _valid_at(node, timestamp):
            disposition = "filtered_stale"
        elif node_id in selected_set:
            disposition = "duplicate"
        elif len(selected) >= config["top_k"]:
            disposition = "dropped_top_k"
        else:
            token_count = _tokens(node["summary"])
            if used_tokens + token_count > config["context_token_budget"]:
                disposition = "dropped_budget"
            else:
                disposition = "selected"
                selected.append(node_id)
                selected_set.add(node_id)
                used_tokens += token_count
        candidate_decisions.append(
            {"node_id": node_id, "reason": reason, "disposition": disposition}
        )
        return disposition == "selected"

    for node_id in sorted(case["seed_node_ids"]):
        consider_node(node_id, "retrieval_seed")

    route_selected = graph_enabled and case["graph_utility_reason"] in GRAPH_REASONS
    if not graph_enabled:
        graph_route = {
            "status": "disabled",
            "reason": "matched_graph_off_arm",
            "selected": False,
        }
    elif not route_selected:
        graph_route = {
            "status": "skipped",
            "reason": case["graph_utility_reason"],
            "selected": False,
        }
    else:
        graph_route = {
            "status": "engaged",
            "reason": case["graph_utility_reason"],
            "selected": True,
        }
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            outgoing.setdefault(edge["from_node_id"], []).append(edge)
        queue = deque((node_id, 0) for node_id in selected)
        traversed: set[str] = set()
        while queue:
            from_node_id, depth = queue.popleft()
            if depth >= config["graph_max_hops"]:
                continue
            for edge in sorted(
                outgoing.get(from_node_id, []), key=lambda item: item["edge_id"]
            ):
                if edge["edge_id"] in traversed:
                    continue
                traversed.add(edge["edge_id"])
                target = nodes[edge["to_node_id"]]
                rejected_reason = _edge_reason(edge, target, workspace, timestamp)
                if rejected_reason is not None:
                    edge_decisions.append(
                        {
                            "edge_id": edge["edge_id"],
                            "disposition": "dropped",
                            "reason": rejected_reason,
                        }
                    )
                    continue
                source = sources[edge["source_id"]]
                selected_now = consider_node(edge["to_node_id"], "graph_expansion")
                disposition = (
                    "selected"
                    if edge["to_node_id"] in selected_set
                    else "eligible_not_selected"
                )
                edge_decisions.append(
                    {
                        "edge_id": edge["edge_id"],
                        "disposition": disposition,
                        "reason": "supported",
                    }
                )
                if disposition == "selected":
                    selected_paths.append(
                        {
                            "edge_id": edge["edge_id"],
                            "from_node_id": edge["from_node_id"],
                            "to_node_id": edge["to_node_id"],
                            "relation": edge["relation"],
                            "origin": edge["origin"],
                            "support_state": edge["support_state"],
                            "source_id": edge["source_id"],
                            "source_revision": edge["source_revision"],
                            "source_digest_sha256": sha256_text(source["content"]),
                            "evidence_anchor": edge["evidence_anchor"],
                        }
                    )
                if selected_now:
                    queue.append((edge["to_node_id"], depth + 1))

    selected_source_ids = sorted(
        {
            source_id
            for node_id in selected
            for source_id in nodes[node_id]["source_ids"]
        }
    )
    required = set(case["required_source_ids"])
    selected_sources = set(selected_source_ids)
    covered = len(required & selected_sources)
    all_required = required <= selected_sources
    expected_answer = case["expected_answer"]
    if expected_answer == "abstain":
        answer_verdict = "abstain" if not selected else "incorrect"
    else:
        answer_verdict = "correct" if all_required else "insufficient_evidence"
    selected_unsupported = sum(
        1 for path in selected_paths if path["support_state"] != "supported"
    )
    stale_selected = sum(
        1 for node_id in selected if not _valid_at(nodes[node_id], timestamp)
    )
    dropped_count = sum(
        1 for item in candidate_decisions if item["disposition"] != "selected"
    ) + sum(1 for item in edge_decisions if item["disposition"] == "dropped")
    operation_count = len(candidate_decisions) + len(edge_decisions)
    return {
        "cell_id": cell_id,
        "case_id": case["case_id"],
        "shape": case["shape"],
        "query_sha256": sha256_text(case["query"]),
        "graph_route": graph_route,
        "selected_node_ids": selected,
        "selected_source_ids": selected_source_ids,
        "selected_paths": selected_paths,
        "candidate_decisions": candidate_decisions,
        "edge_decisions": edge_decisions,
        "retrieval_evidence": {
            "required_source_count": len(required),
            "covered_source_count": covered,
            "source_evidence_recall": round(covered / len(required), 6)
            if required
            else 1.0,
            "all_required_evidence": all_required,
            "path_relation_precision": round(
                (len(selected_paths) - selected_unsupported) / len(selected_paths), 6
            )
            if selected_paths
            else 1.0,
            "unsupported_edge_rate": round(
                selected_unsupported / len(selected_paths), 6
            )
            if selected_paths
            else 0.0,
            "stale_conflict_leakage": stale_selected,
        },
        "answer_metrics": {
            "expected": expected_answer,
            "verdict": answer_verdict,
            "matched_expected": answer_verdict == expected_answer,
            "abstained": answer_verdict == "abstain",
        },
        "context_cost": {
            "selected_count": len(selected),
            "dropped_count": dropped_count,
            "delivered_tokens": used_tokens,
            "token_budget": config["context_token_budget"],
        },
        "execution": {"operation_count": operation_count, "errors": 0},
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    required_total = sum(
        row["retrieval_evidence"]["required_source_count"] for row in rows
    )
    covered_total = sum(
        row["retrieval_evidence"]["covered_source_count"] for row in rows
    )
    selected_paths = [path for row in rows for path in row["selected_paths"]]
    selected_unsupported = sum(
        1 for path in selected_paths if path["support_state"] != "supported"
    )
    return {
        "retrieval_evidence": {
            "denominator": required_total,
            "source_evidence_recall": round(covered_total / required_total, 6)
            if required_total
            else 1.0,
            "all_required_evidence_rate": round(
                sum(
                    bool(row["retrieval_evidence"]["all_required_evidence"])
                    for row in rows
                )
                / len(rows),
                6,
            ),
            "path_relation_precision": round(
                (len(selected_paths) - selected_unsupported) / len(selected_paths), 6
            )
            if selected_paths
            else 1.0,
            "unsupported_edge_rate": round(
                selected_unsupported / len(selected_paths), 6
            )
            if selected_paths
            else 0.0,
            "stale_conflict_leakage_count": sum(
                row["retrieval_evidence"]["stale_conflict_leakage"] for row in rows
            ),
        },
        "answer_quality": {
            "denominator": len(rows),
            "matched_expected_count": sum(
                bool(row["answer_metrics"]["matched_expected"]) for row in rows
            ),
            "matched_expected_rate": round(
                sum(bool(row["answer_metrics"]["matched_expected"]) for row in rows)
                / len(rows),
                6,
            ),
            "abstention_count": sum(
                bool(row["answer_metrics"]["abstained"]) for row in rows
            ),
        },
        "context_cost": {
            "token_denominator": len(rows),
            "delivered_tokens": sum(
                row["context_cost"]["delivered_tokens"] for row in rows
            ),
            "selected_count": sum(
                row["context_cost"]["selected_count"] for row in rows
            ),
            "dropped_count": sum(row["context_cost"]["dropped_count"] for row in rows),
        },
        "execution": {
            "provider_calls": 0,
            "network_calls": 0,
            "error_denominator": len(rows),
            "errors": sum(row["execution"]["errors"] for row in rows),
            "operation_count": sum(row["execution"]["operation_count"] for row in rows),
            "latency": {
                "status": "not_measured_provider_free",
                "denominator": 0,
                "total_ms": 0,
            },
        },
    }


def build_report(fixture: dict[str, Any]) -> dict[str, Any]:
    normalized = _validate_fixture(fixture)
    sources = {item["source_id"]: item for item in normalized["sources"]}
    nodes = {item["node_id"]: item for item in normalized["nodes"]}
    edges = normalized["edges"]
    matched_config = normalized["matched_config"]
    matched_config_sha256 = sha256_json(matched_config)
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for cell_id, graph_enabled in (("graph-off", False), ("graph-on", True)):
        cell_rows = [
            _run_case(
                case,
                cell_id=cell_id,
                graph_enabled=graph_enabled,
                fixture=normalized,
                sources=sources,
                nodes=nodes,
                edges=edges,
            )
            for case in normalized["cases"]
        ]
        rows.extend(cell_rows)
        config = {**matched_config, "graph_enabled": graph_enabled}
        cells.append(
            {
                "cell_id": cell_id,
                "config": config,
                "matched_config_sha256": matched_config_sha256,
                "cell_config_sha256": sha256_json(config),
                "metrics": _aggregate(cell_rows),
            }
        )
    rows.sort(key=lambda row: (row["cell_id"], row["case_id"]))
    cells.sort(key=lambda cell: cell["cell_id"])
    source_projection = _source_projection(sources)
    fixture_coverage = sorted(REQUIRED_SHAPES | {"abstention"})
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "status": "complete",
        "benchmark": "matched-graph-context-ablation",
        "fixture_id": normalized["fixture_id"],
        "fixture_coverage": fixture_coverage,
        "corpus": {
            "source_count": len(sources),
            "node_count": len(nodes),
            "edge_count": len(edges),
            "case_count": len(normalized["cases"]),
            "source_types": sorted(
                {source["source_type"] for source in sources.values()}
            ),
            "sources": source_projection,
        },
        "comparison": {
            "intended_difference": "graph_enabled",
            "mode_comparison_included": False,
            "paired_controls": [
                "query",
                "corpus",
                "retrieval_mode",
                "top_k",
                "context_token_budget",
                "reader",
                "prompt",
                "judge",
                "seed",
            ],
        },
        "cells": cells,
        "rows": rows,
        "commitments": {
            "fixture_sha256": sha256_json(normalized),
            "dataset_sha256": sha256_json(
                {
                    "sources": source_projection,
                    "nodes": normalized["nodes"],
                    "edges": normalized["edges"],
                    "cases": normalized["cases"],
                }
            ),
            "manifest_sha256": sha256_json(
                {
                    "schema_version": REPORT_SCHEMA,
                    "fixture_id": normalized["fixture_id"],
                    "matched_config": matched_config,
                }
            ),
            "matched_config_sha256": matched_config_sha256,
            "prompt_sha256": sha256_text(matched_config["prompt"]),
            "offline_reader_sha256": sha256_text(matched_config["reader"]),
            "offline_judge_sha256": sha256_text(matched_config["judge"]),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "execution": {
            "mode": "provider-free-deterministic",
            "provider_calls": 0,
            "network_calls": 0,
            "paid": False,
            "raw_external_payloads_captured": False,
        },
        "comparability": {
            "label": "vault-owned-synthetic-diagnostic",
            "third_party_score_comparison_allowed": False,
            "claims": [
                "fixture-level graph utility and evidence coverage only",
                "not model-internal causality",
                "not third-party benchmark efficacy",
            ],
        },
    }
    report["report_sha256"] = sha256_json(report)
    validate_report(report)
    return report


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).lower()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def validate_report(report: dict[str, Any]) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_SCHEMA:
        raise ContractError("unsupported report schema")
    if report.get("status") != "complete":
        raise ContractError("report is not complete")
    supplied = report.get("report_sha256")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise ContractError("report digest is missing or invalid")
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    if supplied != sha256_json(unsigned):
        raise ContractError("report digest mismatch")
    execution = report.get("execution", {})
    if (
        execution.get("provider_calls") != 0
        or execution.get("network_calls") != 0
        or execution.get("paid") is not False
    ):
        raise ContractError("provider-free execution contract violated")
    if set(FORBIDDEN_REPORT_KEYS) & set(_walk_keys(report)):
        raise ContractError("report contains a forbidden raw or secret field")
    rows = report.get("rows")
    cells = report.get("cells")
    if (
        not isinstance(rows, list)
        or not rows
        or not isinstance(cells, list)
        or len(cells) != 2
    ):
        raise ContractError("report requires paired cells and rows")
    for cell in cells:
        cell_rows = [row for row in rows if row.get("cell_id") == cell.get("cell_id")]
        if cell.get("metrics") != _aggregate(cell_rows):
            raise ContractError("cell metrics do not match rows")
    for row in rows:
        for path in row.get("selected_paths", []):
            if path.get("support_state") != "supported":
                raise ContractError("selected path is unsupported")
            for field in ("source_id", "source_revision", "evidence_anchor"):
                if not isinstance(path.get(field), str) or not path[field]:
                    raise ContractError("selected path lacks source evidence")
            if not re.fullmatch(
                r"[0-9a-f]{64}", str(path.get("source_digest_sha256", ""))
            ):
                raise ContractError("selected path source digest is invalid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture", default=str(Path(__file__).with_name("fixture.json"))
    )
    parser.add_argument("--out", default=str(Path(__file__).with_name("report.json")))
    args = parser.parse_args(argv)
    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    report = build_report(fixture)
    validate_report(report)
    Path(args.out).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"status": report["status"], "report_sha256": report["report_sha256"]},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
