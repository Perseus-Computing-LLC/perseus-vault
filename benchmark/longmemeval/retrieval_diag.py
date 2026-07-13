#!/usr/bin/env python3
"""Offline, judge-free retrieval coverage diagnostic for LongMemEval (#580).

Replays the EXACT benchmark retrieval path (identical per-instance ingest +
hybrid recall as `qa.py`'s `build_context`) for every question, at a deep
top-K, and records where each gold evidence session (`answer_session_ids`)
ranked. From that it computes gold-evidence **coverage@k** — the fraction of
questions whose gold sessions are all within the top-k — for a ladder of k
values.

Why this exists
---------------
QA accuracy conflates two failures: (a) the evidence was never retrieved, and
(b) it was retrieved but the model reasoned over it wrong. This diagnostic
isolates (a) with **no LLM, no judge, no API cost** — so a retrieval change
gets a fast, deterministic recall gate instead of a $35 QA run. It is the
measurement companion to the #579 CoT work (which addresses (b)).

It also doubles as a **coverage regression guard**: run it in CI (or locally)
with `--min-coverage-at 20:0.95` to fail when coverage@20 drops below a floor.

Run it
------
    cargo build --release
    python benchmark/longmemeval/retrieval_diag.py \
        --data longmemeval_s_cleaned.json \
        --bin target/release/perseus-vault \
        --k 50 --out diag.json --journal diag.jsonl

No API key, no network, no LLM. ~6 min for the full 500 on a laptop; resumable
via --journal (one JSON line per question, config-pinned header).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run import MimirServer, session_text, find_binary  # noqa: E402

from datetime import datetime

SHARED_FACT_KEY = "__ku_fact__"


def to_ms(datestr):
    """LongMemEval session date ('2023/08/11 (Fri) 00:01') -> unix ms.
    Keeps the time-of-day: same-day updates must still order correctly."""
    s = re.sub(r"\s*\([^)]*\)\s*", " ", datestr).strip()
    try:
        d = datetime.strptime(s, "%Y/%m/%d %H:%M")
    except ValueError:
        d = datetime.strptime(s.split(" ")[0], "%Y/%m/%d")
    return int(d.timestamp() * 1000)


def session_note(date, turns):
    """Identical to qa.py::session_note — the ingested per-session body must
    match the benchmark exactly for the diagnostic to be faithful."""
    prefix = f"session date: {date}\n" if date else ""
    return prefix + session_text(turns)


def gold_ranks(inst, srv, qid, k, ku_shared=False):
    """Ingest this instance's haystack and hybrid-recall top-k, exactly as
    qa.py does. Return (ranks, n_sessions, update_id) where ranks maps each
    gold session id to its 1-based rank in the top-k results (absent => not
    present) and update_id is the latest-dated gold session (None if <2 dated).

    With ku_shared, the gold (fact-version) sessions are instead ingested
    under ONE shared key with valid_from = session date — the PRODUCT shape,
    where `mimir_remember` collapses versions to a live latest-wins row (see
    INGEST_590.md demo B). Grouping uses the dataset's evidence labels: which
    sessions update the same fact is authoring-time knowledge, exactly what a
    real caller has when it re-remembers a fact under its key. Stale versions
    are then in `entity_history`, not the live index, so their ranks are None
    by construction; the shared key's rank is attributed to the update gold."""
    sessions = inst["haystack_sessions"]
    sids = inst["haystack_session_ids"]
    dates = inst.get("haystack_dates") or [None] * len(sids)
    by_id = {sid: (turns, d) for sid, turns, d in zip(sids, sessions, dates)}

    gold = inst.get("answer_session_ids", []) or []
    dated_gold = sorted([g for g in gold if by_id.get(g, (None, None))[1]],
                        key=lambda g: to_ms(by_id[g][1]))
    update_id = dated_gold[-1] if len(dated_gold) >= 2 else None
    shared = set(dated_gold) if (ku_shared and update_id) else set()

    for sid in sids:
        if sid in shared:
            continue  # fact versions ingest below, ascending by date
        turns, d = by_id[sid]
        srv.call("mimir_remember", {"category": qid, "key": sid,
                                    "body_json": json.dumps({"note": session_note(d, turns)}),
                                    "type": "fact"})
    for g in dated_gold if shared else []:
        turns, d = by_id[g]
        srv.call("mimir_remember", {"category": qid, "key": SHARED_FACT_KEY,
                                    "body_json": json.dumps({"note": session_note(d, turns)}),
                                    "type": "fact", "valid_from_unix_ms": to_ms(d)})
    srv.call("mimir_embed", {"batch_category": qid, "batch_limit": 1000})
    r = srv.call("mimir_recall", {"query": inst["question"], "mode": "hybrid",
                                  "category": qid, "limit": k, "trust_weight": 0,
                                  "min_decay": 0})
    items = r.get("items", []) if isinstance(r, dict) else []
    ranked_ids = [it.get("key") for it in items]
    pos = {sid: i + 1 for i, sid in enumerate(ranked_ids)}

    ranks = {g: pos.get(g) for g in gold}
    if shared:
        ranks[update_id] = pos.get(SHARED_FACT_KEY)
    return ranks, len(sids), update_id


def coverage_at(records, k):
    """Fraction of questions (with >=1 gold session) whose gold sessions are
    ALL ranked <= k."""
    scored = [rec for rec in records if rec["gold"]]
    if not scored:
        return None
    covered = 0
    for rec in scored:
        rr = [rec["ranks"].get(g) for g in rec["gold"]]
        if all(r is not None and r <= k for r in rr):
            covered += 1
    return round(covered / len(scored), 4)


def coverage_latest_at(records, k):
    """Latest-version coverage@k: for version-bearing questions (>=2 dated
    golds) only the LATEST gold session must rank <= k — the version a
    knowledge-update answer actually needs; other questions use the standard
    all-gold rule. Comparable across benchmark-shape and --ku-shared-key runs
    (where stale versions are in history by construction)."""
    scored = [rec for rec in records if rec["gold"]]
    if not scored:
        return None
    covered = 0
    for rec in scored:
        upd = rec.get("update_gold")
        if upd:
            r = rec["ranks"].get(upd)
            covered += 1 if (r is not None and r <= k) else 0
        else:
            rr = [rec["ranks"].get(g) for g in rec["gold"]]
            covered += 1 if all(r is not None and r <= k for r in rr) else 0
    return round(covered / len(scored), 4)


def parse_floor(spec):
    """'20:0.95' -> (20, 0.95)."""
    try:
        k_str, cov_str = spec.split(":")
        return int(k_str), float(cov_str)
    except Exception:
        raise argparse.ArgumentTypeError(
            f"--min-coverage-at expects K:FRACTION (e.g. 20:0.95), got {spec!r}")


def main():
    ap = argparse.ArgumentParser(description="LongMemEval retrieval coverage diagnostic (offline, judge-free)")
    ap.add_argument("--data", default=None,
                    help="Path to longmemeval_<split>_cleaned.json (default: ./longmemeval_s_cleaned.json)")
    ap.add_argument("--split", default="s", choices=["s", "m"])
    ap.add_argument("--k", type=int, default=50, help="Depth to retrieve and score against (default 50)")
    ap.add_argument("--ladder", default="5,10,20,30,50",
                    help="Comma-separated k values to report coverage@k for (default 5,10,20,30,50)")
    ap.add_argument("--limit", type=int, default=0, help="Only run the first N instances (0 = all)")
    ap.add_argument("--only-types", nargs="+", default=None, metavar="TYPE",
                    help="Restrict to these question_type categories")
    ap.add_argument("--ku-shared-key", action="store_true",
                    help="PRODUCT-shape ingest for version-bearing questions: gold fact-version "
                         "sessions share one key with valid_from = session date (latest-wins; "
                         "stale versions live in entity_history). See INGEST_590.md.")
    ap.add_argument("--bin", default=None, help="perseus-vault binary (else auto-located / MIMIR_BIN)")
    ap.add_argument("--out", default=str(HERE / "diag_report.json"))
    ap.add_argument("--journal", default=None, help="Crash-safe per-question journal (resumable)")
    ap.add_argument("--resume", action="store_true", help="Resume from --journal")
    ap.add_argument("--min-coverage-at", type=parse_floor, default=None, metavar="K:FRAC",
                    help="Regression gate: exit non-zero if coverage@K < FRAC (e.g. 20:0.95)")
    args = ap.parse_args()

    data_path = Path(args.data) if args.data else HERE / f"longmemeval_{args.split}_cleaned.json"
    if not data_path.exists():
        sys.exit(f"error: dataset not found: {data_path}")
    full = json.loads(data_path.read_text(encoding="utf-8"))
    split_size = len(full)
    if args.only_types:
        only = set(args.only_types)
        full = [i for i in full if i.get("question_type") in only]
    data = full[: args.limit] if args.limit else full

    ladder = [int(x) for x in args.ladder.split(",") if x.strip()]

    binary = find_binary(args.bin)
    db = str(Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp") / "mimir-diag.db")

    def wipe():
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db + ext)
            except OSError:
                pass

    run_config = {"split": args.split, "n": len(data), "k": args.k,
                  "only_types": sorted(args.only_types) if args.only_types else None,
                  "ku_shared_key": args.ku_shared_key}

    # ── crash-safe journal + resume (same convention as qa.py) ──────────────
    journal_path = Path(args.journal) if args.journal else None
    done = {}
    journal = None
    records = []
    if journal_path:
        resume_ok = False
        if args.resume and journal_path.exists():
            lines = [json.loads(ln) for ln in
                     journal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if not lines or "_config" not in lines[0]:
                sys.exit(f"error: --resume: {journal_path} has no config header")
            if lines[0]["_config"] != run_config:
                sys.exit("error: --resume config mismatch:\n"
                         f"  journal: {lines[0]['_config']}\n  current: {run_config}")
            for rec in lines[1:]:
                done[rec["question_id"]] = rec
                records.append(rec)
            resume_ok = True
            print(f"  resume: {len(done)} questions reloaded from {journal_path.name}")
        journal = open(journal_path, "a" if resume_ok else "w", encoding="utf-8")
        if not resume_ok:
            journal.write(json.dumps({"_config": run_config}) + "\n")
            journal.flush()

    total = len(data)
    for idx, inst in enumerate(data):
        qid = inst["question_id"]
        if qid in done:
            continue
        wipe()
        srv = MimirServer(binary, db)
        try:
            ranks, n_sess, update_id = gold_ranks(inst, srv, qid, args.k,
                                                  ku_shared=args.ku_shared_key)
        finally:
            srv.close()
        rec = {
            "question_id": qid,
            "question_type": inst.get("question_type", "unknown"),
            "gold": list(inst.get("answer_session_ids", []) or []),
            "update_gold": update_id,
            "ranks": ranks,
            "n_haystack_sessions": n_sess,
        }
        records.append(rec)
        if journal:
            journal.write(json.dumps(rec) + "\n")
            journal.flush()
        if (idx + 1) % 25 == 0:
            print(f"  {idx + 1}/{total} …", file=sys.stderr)

    if journal:
        journal.close()

    # ── coverage ladder + miss buckets ──────────────────────────────────────
    scored = [r for r in records if r["gold"]]
    coverage = {f"@{k}": coverage_at(records, k) for k in ladder}
    coverage_latest = {f"@{k}": coverage_latest_at(records, k) for k in ladder}

    # Per-question worst gold rank (None if any gold session absent from top-k).
    def worst_rank(rec):
        rr = [rec["ranks"].get(g) for g in rec["gold"]]
        return None if any(r is None for r in rr) else max(rr)

    k_recoverable = []   # all gold found but worst rank > 10 (recoverable by deeper k)
    hard = []            # >=1 gold session absent from top-k entirely
    for rec in scored:
        wr = worst_rank(rec)
        if wr is None:
            hard.append(rec["question_id"])
        elif wr > 10:
            k_recoverable.append({"question_id": rec["question_id"], "worst_rank": wr,
                                  "question_type": rec["question_type"]})

    report = {
        "benchmark": "perseus-vault-longmemeval-retrieval-coverage",
        "metric": "gold-evidence coverage@k (offline, judge-free)",
        "dataset": data_path.name,
        "split": f"longmemeval_{args.split}",
        "split_size": split_size,
        "n_instances": total,
        "n_scored": len(scored),
        "retrieval": {"mode": "hybrid", "k": args.k, "trust_weight": 0, "min_decay": 0},
        "ingest_shape": "ku-shared-key (product)" if args.ku_shared_key else "unique-key-per-session (benchmark)",
        "coverage_at_k": coverage,
        "coverage_latest_at_k": coverage_latest,
        "k_recoverable": sorted(k_recoverable, key=lambda x: x["worst_rank"]),
        "hard_misses": sorted(hard),
        "binary": Path(binary).name,
        "platform": platform.platform(),
        "offline": True,
    }
    sig = hashlib.sha256(json.dumps({
        "coverage": coverage, "coverage_latest": coverage_latest, "hard": sorted(hard),
        "n": total, "k": args.k, "ku_shared_key": args.ku_shared_key,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    report["signature_sha256"] = sig
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"\nRetrieval coverage — {len(scored)} scored / {total} instances "
          f"(mode=hybrid, k={args.k}, offline"
          f"{', ingest=ku-shared-key' if args.ku_shared_key else ''})")
    for k in ladder:
        c = coverage[f"@{k}"]
        cl = coverage_latest[f"@{k}"]
        line = (f"  coverage@{k:<3} = {c*100:.1f}%" if c is not None else f"  coverage@{k:<3} = n/a")
        if cl is not None:
            line += f"   latest-version = {cl*100:.1f}%"
        print(line)
    print(f"  k-recoverable (gold ranked 11-{args.k}): {len(k_recoverable)}")
    print(f"  hard misses  (a gold session absent from top-{args.k}): {len(hard)}")
    print(f"  signature: {sig[:16]}...  ->  {args.out}")

    # Regression gate (optional).
    if args.min_coverage_at:
        gate_k, floor = args.min_coverage_at
        actual = coverage_at(records, gate_k)
        if actual is None:
            sys.exit("error: --min-coverage-at: no scored questions to gate on")
        if actual < floor:
            print(f"\nFAIL: coverage@{gate_k} = {actual*100:.1f}% < floor {floor*100:.1f}%",
                  file=sys.stderr)
            return 1
        print(f"\nPASS: coverage@{gate_k} = {actual*100:.1f}% >= floor {floor*100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
