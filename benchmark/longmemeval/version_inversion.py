#!/usr/bin/env python3
"""Version-inversion diagnostic for knowledge-update questions (#590).

Knowledge-update is the one LongMemEval category where the product arm (top-k
hybrid recall) trailed full-context stuffing on the #749 stratified
certification. The hypothesis (issue #590): these questions ask for the LATEST
value of a fact that appears in several sessions with *different* values over
time. Relevance-ranked recall surfaces the outdated session as prominently as
(or above) the update, so the answerer sees the stale value and picks it —
while full-context accidentally wins by seeing every session in temporal order.

This script isolates that failure mode with **no LLM, no judge, no API cost**.
It consumes the journal written by ``retrieval_diag.py`` (the signed retrieval
harness) plus the dataset's per-session dates, and measures the
**version-inversion rate**: among knowledge-update questions whose gold evidence
spans >=2 dated sessions, how often the earlier ("stale") gold session is ranked
at or above the later ("update") gold session within top-k — the exact condition
under which the model is fed the wrong version.

Recall (coverage@k) is deliberately NOT the metric here: coverage@10 on
knowledge-update is ~97%, so the gold sessions are almost always retrieved. This
diagnostic measures ORDER, which is what a recency prior would change.

Usage
-----
    # 1) produce the ranks journal with the signed harness (free, offline):
    python benchmark/longmemeval/retrieval_diag.py \
        --data longmemeval_s_cleaned.json --bin target/release/perseus-vault \
        --k 20 --only-types knowledge-update --journal ku_diag.jsonl

    # 2) score version-inversion off that journal:
    python benchmark/longmemeval/version_inversion.py \
        --data longmemeval_s_cleaned.json --journal ku_diag.jsonl --k 10

Exit code is non-zero when the inversion rate exceeds ``--max-rate`` (a CI floor,
so a recency change can be gated on lowering it).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_dataset(path):
    with open(path, encoding="utf-8") as f:
        return {d["question_id"]: d for d in json.load(f)}


def _date_map(inst):
    return dict(zip(inst["haystack_session_ids"], inst["haystack_dates"]))


def score(dataset, journal_path, k):
    recs = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if "_config" in r:
                continue
            recs.append(r)

    versioned = 0
    inversions = 0        # stale gold ranked above the update gold within top-k
    update_missing = 0    # update gold absent from top-k while a stale one is present
    cases = []
    for r in recs:
        inst = dataset.get(r["question_id"])
        if not inst:
            continue
        dm = _date_map(inst)
        dated = [(g, dm.get(g)) for g in r["gold"] if dm.get(g)]
        if len(dated) < 2:
            continue  # single-version gold: no inversion possible
        dated.sort(key=lambda x: x[1])          # by session date, ascending
        stale_ids = [g for g, _ in dated[:-1]]
        update_id = dated[-1][0]
        versioned += 1
        ranks = r["ranks"]
        upd_rank = ranks.get(update_id)
        stale_in = [ranks[s] for s in stale_ids if ranks.get(s) is not None and ranks[s] <= k]
        upd_in = upd_rank is not None and upd_rank <= k
        if stale_in and not upd_in:
            update_missing += 1
            cases.append((r["question_id"], "update-absent", upd_rank, sorted(stale_in)))
        elif stale_in and upd_in and min(stale_in) < upd_rank:
            inversions += 1
            cases.append((r["question_id"], "stale-above-update", upd_rank, sorted(stale_in)))
    total = inversions + update_missing
    rate = total / versioned if versioned else 0.0
    return {
        "questions_scored": len(recs),
        "version_bearing": versioned,
        "inversions_top_k": inversions,
        "update_absent": update_missing,
        "total_inversion_cases": total,
        "inversion_rate": round(rate, 4),
        "k": k,
        "cases": cases,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="LongMemEval dataset JSON")
    ap.add_argument("--journal", required=True, help="retrieval_diag.py --journal output")
    ap.add_argument("--k", type=int, default=10, help="top-k window (default 10 = product config)")
    ap.add_argument("--max-rate", type=float, default=None,
                    help="CI floor: exit non-zero if inversion_rate exceeds this")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    args = ap.parse_args()

    rep = score(_load_dataset(args.data), args.journal, args.k)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print(f"\n  version-inversion — knowledge-update, top-{rep['k']} (offline, no LLM)\n")
        print(f"    questions scored:        {rep['questions_scored']}")
        print(f"    version-bearing (>=2):   {rep['version_bearing']}")
        print(f"    stale ranked above upd:  {rep['inversions_top_k']}")
        print(f"    update absent<=k:        {rep['update_absent']}")
        print(f"    TOTAL inversion cases:   {rep['total_inversion_cases']}"
              f"  ({100 * rep['inversion_rate']:.1f}%)")
    if args.max_rate is not None and rep["inversion_rate"] > args.max_rate:
        print(f"\n  ✗ inversion_rate {rep['inversion_rate']:.3f} exceeds floor {args.max_rate:.3f}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
