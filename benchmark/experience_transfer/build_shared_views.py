#!/usr/bin/env python3
"""Build the shared corpus projection for independent implementations."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from .common import SHARED_VIEWS_SCHEMA, canonical_digest, validate_corpus, validate_shared_views
except ImportError:
    from common import SHARED_VIEWS_SCHEMA, canonical_digest, validate_corpus, validate_shared_views


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("corpus/corpus.json"))
    parser.add_argument("--out", type=Path, default=Path("corpus/shared_agent_views.json"))
    parser.add_argument("--labels-out", type=Path, default=Path("corpus/label_commitments.json"))
    args = parser.parse_args()
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    rows = [
        {
            "case_id": case["case_id"],
            "pair_id": case["pair_id"],
            "deterministic_seed": case["deterministic_seed"],
            "agent_view": case["agent_view"],
            "agent_view_sha256": canonical_digest(case["agent_view"]),
        }
        for case in corpus["cases"]
    ]
    bundle = {
        "schema": SHARED_VIEWS_SCHEMA,
        "seed": corpus["seed"],
        "pair_count": corpus["pair_count"],
        "public_boundary": "shared agent views contain no expected labels; evaluator label commitments are separate",
        "cases": rows,
    }
    validate_shared_views(bundle)
    label_commitments = {
        "schema": "verified-experience-transfer-label-commitments/v1",
        "seed": corpus["seed"],
        "pair_count": corpus["pair_count"],
        "public_boundary": "decision labels are withheld; only commitments are emitted",
        "cases": [
            {
                "case_id": case["case_id"],
                "pair_id": case["pair_id"],
                "label_sha256": case["commitments"]["label_sha256"],
            }
            for case in corpus["cases"]
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.labels_out.write_text(json.dumps(label_commitments, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = args.out.parent / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"corpus manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["shared_views_file_sha256"] = file_sha(args.out)
    manifest["label_commitments_file_sha256"] = file_sha(args.labels_out)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"shared_views": str(args.out), "shared_views_sha256": file_sha(args.out), "label_commitments": str(args.labels_out), "labels_sha256": file_sha(args.labels_out), "manifest_sha256": file_sha(manifest_path), "cases": len(rows)}, sort_keys=True))


if __name__ == "__main__":
    main()
