#!/usr/bin/env python3
"""Per-question-type coverage@k breakdown from a retrieval_diag.py journal.

retrieval_diag.py reports overall coverage@k + hard-miss / k-recoverable
buckets, but for the #588/#590 before/after tables we need coverage sliced by
`question_type` (multi-session, temporal-reasoning, knowledge-update, …) and
the rank of specific target question_ids. This is a pure post-processor over
the journal (one JSON line per question); it runs no binary and costs nothing,
so it can be re-run on any before/after journal to build the comparison tables.

    python cov_by_type.py --journal baseline_diag.jsonl \
        --ladder 5,10,20,30,50 [--track id1 id2 ...] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load(journal):
    recs = []
    for ln in Path(journal).read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if "_config" in r:
            continue
        recs.append(r)
    return recs


def worst_rank(rec):
    rr = [rec["ranks"].get(g) for g in rec["gold"]]
    if any(r is None for r in rr):
        return None
    return max(rr)


def coverage_at(recs, k):
    scored = [r for r in recs if r["gold"]]
    if not scored:
        return None
    cov = sum(1 for r in scored
              if all(rk is not None and rk <= k for rk in
                     (r["ranks"].get(g) for g in r["gold"])))
    return cov / len(scored)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", required=True)
    ap.add_argument("--ladder", default="5,10,20,30,50")
    ap.add_argument("--track", nargs="*", default=[],
                    help="question_ids to print gold ranks for")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    recs = load(args.journal)
    ladder = [int(x) for x in args.ladder.split(",") if x.strip()]
    by_type = defaultdict(list)
    for r in recs:
        by_type[r.get("question_type", "unknown")].append(r)

    print(f"\n  coverage@k by question_type  ({len(recs)} questions, journal={Path(args.journal).name})\n")
    hdr = f"    {'type':<26}{'n':>4}" + "".join(f"{'@'+str(k):>8}" for k in ladder) + f"{'hard':>7}"
    print(hdr)
    print("    " + "-" * (len(hdr) - 4))
    out = {"by_type": {}, "overall": {}}
    for qt in sorted(by_type):
        rs = by_type[qt]
        scored = [r for r in rs if r["gold"]]
        hard = sum(1 for r in scored if worst_rank(r) is None)
        cells = "".join(f"{coverage_at(rs, k)*100:7.1f} " if coverage_at(rs, k) is not None
                        else f"{'n/a':>8}" for k in ladder)
        print(f"    {qt:<26}{len(scored):>4}{cells}{hard:>7}")
        out["by_type"][qt] = {"n": len(scored),
                              **{f"@{k}": round(coverage_at(rs, k), 4) for k in ladder},
                              "hard_misses": hard}
    # overall
    scored_all = [r for r in recs if r["gold"]]
    hard_all = sum(1 for r in scored_all if worst_rank(r) is None)
    cells = "".join(f"{coverage_at(recs, k)*100:7.1f} " for k in ladder)
    print("    " + "-" * (len(hdr) - 4))
    print(f"    {'ALL':<26}{len(scored_all):>4}{cells}{hard_all:>7}")
    out["overall"] = {"n": len(scored_all),
                      **{f"@{k}": round(coverage_at(recs, k), 4) for k in ladder},
                      "hard_misses": hard_all}

    if args.track:
        print("\n  tracked question_ids (gold session -> rank; '-' = absent from top-k):")
        idx = {r["question_id"]: r for r in recs}
        for qid in args.track:
            r = idx.get(qid)
            if not r:
                print(f"    {qid}: NOT IN JOURNAL")
                continue
            ranks = "  ".join(f"{g}={r['ranks'].get(g) if r['ranks'].get(g) is not None else '-'}"
                              for g in r["gold"])
            print(f"    {qid} [{r['question_type']}]: {ranks}")
            out.setdefault("tracked", {})[qid] = {"question_type": r["question_type"],
                                                  "ranks": r["ranks"]}
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(f"\n  -> {args.json}")


if __name__ == "__main__":
    main()
