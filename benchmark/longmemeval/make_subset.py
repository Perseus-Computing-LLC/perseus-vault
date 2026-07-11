#!/usr/bin/env python3
"""make_subset.py — deterministic stratified subset of LongMemEval for paid arms.

The full 500-question `longmemeval_s` run of the expensive `fullcontext` arm
(~113k tokens/question) costs ~$140 at gpt-4o pricing — outside the harness
budget. This script cuts a *stratified* subset (proportional per question_type,
fixed seed, sorted question_ids) so the cheap arms can be compared against the
existing signed 500-question mimir run on the SAME questions: mimir's subset
accuracy is recomputable from report.json's per_question verdicts, so the
product arm costs nothing to re-measure.

Deterministic: same --n and --seed always emit the same subset (question_ids
are sorted within each stratum before sampling). The manifest (question_ids +
per-type counts + sha256 of the subset file) is printed and written next to
the output so a reviewer can verify no cherry-picking occurred.

Usage:
  python make_subset.py --data longmemeval_s_cleaned.json --n 100 \
      --out longmemeval_s_subset100.json
"""
import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="Full split file (longmemeval_s_cleaned.json)")
    ap.add_argument("--n", type=int, default=100, help="Subset size (default 100)")
    ap.add_argument("--seed", type=int, default=475, help="RNG seed (default 475, the tracking issue)")
    ap.add_argument("--out", default=None, help="Output path (default longmemeval_s_subset<N>.json)")
    args = ap.parse_args()

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    out_path = Path(args.out or f"longmemeval_s_subset{args.n}.json")

    by_type = defaultdict(list)
    for inst in data:
        by_type[inst.get("question_type", "unknown")].append(inst)
    for insts in by_type.values():
        insts.sort(key=lambda i: i["question_id"])   # order-independence

    total = len(data)
    rng = random.Random(args.seed)
    # Largest-remainder proportional allocation: strata quotas sum to exactly n.
    quotas = {t: args.n * len(v) / total for t, v in by_type.items()}
    counts = {t: int(q) for t, q in quotas.items()}
    for t in sorted(quotas, key=lambda t: quotas[t] - counts[t], reverse=True):
        if sum(counts.values()) >= args.n:
            break
        counts[t] += 1

    subset = []
    for t in sorted(by_type):
        subset.extend(rng.sample(by_type[t], min(counts[t], len(by_type[t]))))
    subset.sort(key=lambda i: i["question_id"])

    out_path.write_text(json.dumps(subset), encoding="utf-8")
    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()

    manifest = {
        "source": Path(args.data).name,
        "source_n": total,
        "subset_n": len(subset),
        "seed": args.seed,
        "per_type": {t: counts[t] for t in sorted(counts)},
        "subset_sha256": sha,
        "question_ids": [i["question_id"] for i in subset],
    }
    man_path = out_path.with_suffix(".manifest.json")
    man_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({k: manifest[k] for k in
                      ("source", "source_n", "subset_n", "seed", "per_type", "subset_sha256")},
                     indent=2))
    print(f"\nwrote {out_path} + {man_path}")


if __name__ == "__main__":
    main()
