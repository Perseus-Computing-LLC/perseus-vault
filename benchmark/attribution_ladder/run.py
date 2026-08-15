#!/usr/bin/env python3
"""Run the attribution-ladder benchmark (#1049)."""
from __future__ import annotations

import json
from pathlib import Path

from harness import run_benchmark, verify_report, write_outputs


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Attribution-ladder benchmark (#1049)")
    parser.add_argument("--dataset", default=str(Path(__file__).with_name("dataset.json")))
    parser.add_argument("--output", default="benchmark/attribution_ladder/out")
    parser.add_argument("--judge", choices=("deterministic", "llm"), default="deterministic")
    parser.add_argument("--limit", type=int, default=None, help="Cap query count (LLM spend gate)")
    parser.add_argument("--model", default="gpt-4o-mini")
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    report, rows = run_benchmark(dataset, judge=args.judge, limit=args.limit, model=args.model)
    if not verify_report(report, rows):
        raise SystemExit("internal report verification failed")
    write_outputs(Path(args.output), report, rows)
    print(
        json.dumps(
            {
                "output": args.output,
                "rows": len(rows),
                "report_digest": report["signature"]["value"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
