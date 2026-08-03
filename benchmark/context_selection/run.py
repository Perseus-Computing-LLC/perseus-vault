#!/usr/bin/env python3
"""Run the deterministic context-selection-only benchmark fixture."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from harness import run_benchmark, verify_report, write_outputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(Path(__file__).with_name("dataset.json")))
    parser.add_argument("--output", default="benchmark/context_selection/out")
    parser.add_argument("--model-id", default="not-run")
    parser.add_argument("--judge-id", default="not-run")
    args = parser.parse_args()
    dataset = json.loads(Path(args.dataset).read_text())
    report, rows = run_benchmark(dataset, model_id=args.model_id, judge_id=args.judge_id)
    if not verify_report(report, rows):
        raise SystemExit("internal report verification failed")
    write_outputs(args.output, report, rows)
    print(json.dumps({"output": args.output, "rows": len(rows), "report_digest": report["signature"]["value"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
