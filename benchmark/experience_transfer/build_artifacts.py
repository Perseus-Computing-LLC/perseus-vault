#!/usr/bin/env python3
"""Write corpus-level hash commitments without copying fixture contents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from .common import canonical_digest, validate_corpus
except ImportError:
    from common import canonical_digest, validate_corpus


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("corpus/corpus.json"))
    parser.add_argument("--generator", type=Path, default=Path("benchmark/generate_corpus.py"))
    parser.add_argument("--out", type=Path, default=Path("corpus/manifest.json"))
    args = parser.parse_args()
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    manifest: dict[str, Any] = {
        "schema": "verified-experience-transfer-corpus-manifest/v1",
        "corpus_file": str(args.corpus),
        "corpus_sha256": sha(args.corpus),
        "corpus_canonical_sha256": canonical_digest(corpus),
        "generator_file": str(args.generator),
        "generator_sha256": sha(args.generator),
        "seed": corpus["seed"],
        "pair_count": corpus["pair_count"],
        "category_counts": corpus["category_counts"],
        "raw_payloads": "synthetic fixture only; public report contains no task/context bodies",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (args.corpus.parent / "corpus.sha256").write_text(f"{manifest['corpus_sha256']}  {args.corpus.name}\n", encoding="utf-8")
    print(json.dumps({"manifest": str(args.out), "manifest_sha256": sha(args.out), "corpus_sha256": manifest["corpus_sha256"], "canonical_sha256": manifest["corpus_canonical_sha256"]}, sort_keys=True))


if __name__ == "__main__":
    main()
