"""Provider-free conflict-magnet fusion regression contract.

The fixture models only bounded candidate identities and rank lists. It deliberately
contains no prompts, query text, memory bodies, gold labels, or provider output.
The production Rust tests remain the authority for the live RRF implementation;
this module is the benchmark-side adversarial report and publication boundary.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable

SCHEMA_VERSION = "perseus-vault-fusion-regression/v1"
_LIMIT = 2
_RRF_K = 60.0
_FORBIDDEN = frozenset({"conflict_neighbor"})


def _stable(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: object) -> str:
    return hashlib.sha256(_stable(value).encode("utf-8")).hexdigest()


def _rrf(arms: dict[str, list[str]], weights: dict[str, float]) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for arm_name in ("dense", "fts5"):
        weight = weights[arm_name]
        for rank, candidate_id in enumerate(arms[arm_name], start=1):
            scores[candidate_id] = scores.get(candidate_id, 0.0) + weight / (_RRF_K + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def _post_presentation(ids: Iterable[str]) -> list[str]:
    """Move forbidden neighbors after allowed candidates, after fusion only."""
    allowed = [candidate_id for candidate_id in ids if candidate_id not in _FORBIDDEN]
    forbidden = [candidate_id for candidate_id in ids if candidate_id in _FORBIDDEN]
    return allowed + forbidden


def _rank(ids: list[str]) -> dict[str, int]:
    return {candidate_id: rank for rank, candidate_id in enumerate(ids, start=1)}


def _metadata_control() -> dict[str, object]:
    raw_scores = {"relevant": 1.0, "weak_metadata": 0.95, "arm_only": 0.70}
    # Positive hygiene modulation is presentation metadata, not a license to
    # amplify a weak raw relevance score. The v1 contract caps it at 1.0.
    multipliers = {candidate_id: 1.0 for candidate_id in raw_scores}
    adjusted = {
        candidate_id: score * multipliers[candidate_id]
        for candidate_id, score in raw_scores.items()
    }
    adjusted_ids = [
        candidate_id
        for candidate_id, _ in sorted(adjusted.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "raw_rank": _rank(list(raw_scores)),
        "adjusted_rank": _rank(adjusted_ids),
        "positive_multiplier_cap": 1.0,
        "weak_metadata_multiplier": multipliers["weak_metadata"],
    }


def _trace(arms: dict[str, list[str]], final_ids: list[str]) -> dict[str, object]:
    return {
        arm_name: {
            "raw_rank": _rank(ids),
            "adjusted_rank": _rank(ids),
            "final_delivered_rank": {
                candidate_id: rank
                for rank, candidate_id in enumerate(final_ids, start=1)
                if candidate_id in ids
            },
        }
        for arm_name, ids in arms.items()
    }


def canonical_fingerprint(report: dict[str, object]) -> str:
    payload = dict(report)
    payload.pop("signature_sha256", None)
    return hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()


def run_conflict_magnet_fixture() -> dict[str, object]:
    raw_arms = {
        "dense": ["relevant", "arm_only", "conflict_neighbor", "distractor"],
        "fts5": ["conflict_neighbor", "relevant", "distractor", "arm_only"],
    }
    bad_arms = {
        "dense": ["conflict_neighbor", "relevant", "arm_only", "distractor"],
        "fts5": ["conflict_neighbor", "relevant", "arm_only", "distractor"],
    }
    weights = {"dense": 1.0, "fts5": 1.0}
    raw_fused = _rrf(raw_arms, weights)
    bad_fused = _rrf(bad_arms, weights)
    raw_ids = [candidate_id for candidate_id, _ in raw_fused]
    bad_ids = [candidate_id for candidate_id, _ in bad_fused]
    fused_slice = raw_ids[:_LIMIT]
    presented_ids = _post_presentation(fused_slice)
    final_ids = presented_ids
    fixture = {"dense": raw_arms["dense"], "fts5": raw_arms["fts5"], "forbidden": sorted(_FORBIDDEN)}
    config = {"rrf_k": _RRF_K, "weights": weights, "limit": _LIMIT, "presentation": "post_slice_v1"}
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "claim_boundary": "synthetic-ranking-contract-only",
        "fixture_sha256": _sha(fixture),
        "corpus_sha256": _sha({"candidate_ids": sorted(set(raw_ids))}),
        "config_sha256": _sha(config),
        "fusion_path": "flat_rrf_raw_arms_v1",
        "rank_stages": ["raw", "adjusted", "fused", "final_delivered"],
        "network_calls": 0,
        "raw_inputs_captured": False,
        "arms": _trace(raw_arms, final_ids),
        "fusion": {
            "rrf_k": _RRF_K,
            "weights": weights,
            "fused_ids": fused_slice,
            "fused_pool_ids": raw_ids,
            "pre_presentation_ids": fused_slice,
            "post_presentation_ids": presented_ids,
        },
        "bad_control": {
            "description": "presentation swap incorrectly applied before fusion",
            "fused_ids": bad_ids[:_LIMIT],
        },
        "metadata_control": _metadata_control(),
        "final": {
            "ids": final_ids,
            "forbidden_neighbor_exposure": sum(candidate_id in _FORBIDDEN for candidate_id in final_ids),
            "positive_recall": int("relevant" in final_ids),
        },
    }
    report["signature_sha256"] = canonical_fingerprint(report)
    return report


if __name__ == "__main__":
    print(json.dumps(run_conflict_magnet_fixture(), indent=2, sort_keys=True))
