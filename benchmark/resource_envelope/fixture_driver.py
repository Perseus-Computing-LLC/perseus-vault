#!/usr/bin/env python3
"""Deterministic no-network driver used only by resource-envelope contract tests."""

from __future__ import annotations

import argparse
import json
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-json", required=True)
    parser.add_argument("--repetition", type=int, required=True)
    args = parser.parse_args()
    case = json.loads(args.case_json)
    scenario = case["scenario"]
    if scenario == "timeout":
        time.sleep(1.0)
    values = {
        "available": (7, 3, 1, 250_000, 768, 96),
        "empty": (0, 0, 0, 80_000, 0, 0),
        "partial": (7, 1, 0, 300_000, 256, 32),
        "unavailable": (0, 0, 0, 0, 0, 0),
        "degraded": (7, 2, 1, 450_000, 512, 64),
        "timeout": (0, 0, 0, 0, 0, 0),
    }
    candidate_count, selected_count, depth, context_ns, output_bytes, output_tokens = (
        values[scenario]
    )
    print(
        json.dumps(
            {
                "outcome_status": scenario,
                "network_calls": 0,
                "workload": {
                    "measurement_overhead_ns": 1_000,
                    "recall_candidate_count": candidate_count,
                    "selected_count": selected_count,
                    "traversal_depth": depth,
                    "context_assembly_ns": context_ns,
                    "output_bytes": output_bytes,
                    "output_tokens": output_tokens,
                },
                "raw_prompt": "collector must drop this field",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
