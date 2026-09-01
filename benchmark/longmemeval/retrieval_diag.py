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
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

from run import PerseusVaultServer, session_text, find_binary  # noqa: E402
from context_assembly import stable_ranked_items  # noqa: E402
from benchmark.admission_fixture import AGENT, WORKSPACE, admitted_remember  # noqa: E402
from benchmark.package.common.replay import (
    build_envelope as build_replay_envelope,
    build_snapshot as build_replay_snapshot,
    normalize_recall_response,
    prepare_recall_preflight,
    replay_envelope as validate_replay_artifact,
    sha256_text as replay_sha256_text,
    stable_json as replay_stable_json,
    validate_recall_preflight,
    recall_status_is_scoreable,
    ReplayValidationError,
)
from benchmark.longmemeval.sufficiency import build_sufficiency_report

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


def _canonical_replay_body(inst, key, item):
    """Use frozen fixture content, excluding provider execution metadata."""
    sids = list(inst.get("haystack_session_ids", []) or [])
    sessions = list(inst.get("haystack_sessions", []) or [])
    dates = list(inst.get("haystack_dates", []) or [])
    by_id = {sid: (turns, dates[index] if index < len(dates) else None)
             for index, (sid, turns) in enumerate(zip(sids, sessions))}
    if key in by_id:
        turns, date = by_id[key]
        return {"note": session_note(date, turns)}
    if key == SHARED_FACT_KEY:
        dated = [(date, turns) for turns, date in zip(sessions, dates) if date]
        if dated:
            date, turns = sorted(dated, key=lambda pair: to_ms(pair[0]))[-1]
            return {"note": session_note(date, turns)}

    if "body_json" in item:
        body = item["body_json"]
    elif "body" in item:
        body = item["body"]
    elif "content" in item:
        body = item["content"]
    else:
        raise ValueError("recall item lacks a replay body")
    if body is None:
        raise ValueError("recall item has a null replay body")
    volatile = {
        "created_at_unix_ms", "updated_at_unix_ms", "last_accessed_unix_ms",
        "retrieval_count", "follow_count", "follow_rate", "miss_count",
    }

    def strip(value):
        if isinstance(value, dict):
            return {name: strip(child) for name, child in value.items() if name not in volatile}
        if isinstance(value, list):
            return [strip(child) for child in value]
        return value

    return strip(body)


def _replay_rows(inst, items):
    """Normalize a provider response into hash-only replay candidate rows."""
    sids = list(inst.get("haystack_session_ids", []) or [])
    dates = dict(zip(sids, inst.get("haystack_dates", []) or []))
    ordered = sorted(
        sids,
        key=lambda sid: (0, to_ms(dates[sid])) if dates.get(sid) else (1, sids.index(sid)),
    )
    positions = {sid: index + 1 for index, sid in enumerate(ordered)}
    rows = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("recall item is not an object")
        if "key" in item:
            key = item["key"]
        elif "id" in item:
            key = item["id"]
        else:
            raise ValueError("recall item lacks a stable key")
        if not isinstance(key, str) or not key:
            raise ValueError("recall item has an invalid key")
        identity = replay_sha256_text(key)
        body = _canonical_replay_body(inst, key, item)
        content = replay_stable_json({"candidate": key, "body": body})
        row = {
            "candidate_id": f"candidate-{identity}",
            "source_ref": f"source-{identity}",
            "content": content,
            "provenance": "vault-recall",
            "wire_rank": item.get("wire_rank", index + 1),
            "original_position": positions.get(key, index + 1),
        }
        score = item.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            semantics = item.get("score_semantics")
            if not isinstance(semantics, str) or not semantics:
                raise ValueError("recall score requires explicit score_semantics")
            row["score"] = score
            row["score_semantics"] = semantics
        rows.append(row)
    return rows


def _make_replay_artifact(inst, qid, items, k, *, split, corpus_sha256, config_sha256, code_sha256, preflight, status=None, reason=None, runtime_binding=None):
    rows = _replay_rows(inst, items)
    snapshot = build_replay_snapshot(rows)
    effective_top_k = min(k, len(rows)) if rows and (status is None or status == "complete") else k
    envelope = build_replay_envelope(
        workspace_id=f"longmemeval:{split}",
        scope=f"question:{qid}",
        fixture_id="longmemeval-retrieval-v1",
        corpus_sha256=corpus_sha256,
        retrieval_profile="longmemeval-hybrid-v1",
        mode="hybrid",
        top_k=effective_top_k,
        cell_id=qid,
        request_sha256=replay_sha256_text(replay_stable_json({"question_id": qid, "question_sha256": replay_sha256_text(str(inst.get("question", "")))})),
        config_sha256=config_sha256,
        code_sha256=code_sha256,
        preflight=preflight,
        context_policy="query-content-fullpool-v1",
        context_policy_version="1",
        snapshot=snapshot,
        candidates=rows,
        sequence_policy="wire_v1",
        status=status,
        reason=reason,
        runtime_binding=runtime_binding,
        allow_synthetic=runtime_binding is None,
    )
    return envelope, snapshot


def gold_ranks(inst, srv, qid, k, ku_shared=False, *, split="s", corpus_sha256=None, config_sha256=None, code_sha256=None, preflight, runtime_binding=None):
    """Ingest this instance's haystack and hybrid-recall top-k, exactly as
    qa.py does. Return (ranks, n_sessions, update_id, replay, snapshot, status).
    Ranks are absent when the wire outcome is unavailable, which is distinct from
    a healthy empty result and is excluded from coverage denominators.

    With ku_shared, dated gold sessions are re-remembered under one shared key;
    stale versions remain in entity_history and the latest version owns the
    shared key's rank."""
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
        admitted_remember(
            srv,
            qid,
            sid,
            json.dumps({"note": session_note(d, turns)}),
            workspace=WORKSPACE,
            agent=AGENT,
        )
    for g in dated_gold if shared else []:
        turns, d = by_id[g]
        admitted_remember(
            srv,
            qid,
            SHARED_FACT_KEY,
            json.dumps({"note": session_note(d, turns)}),
            workspace=WORKSPACE,
            agent=AGENT,
            valid_from_unix_ms=to_ms(d),
        )
    srv.call("perseus_vault_embed", {"batch_category": qid, "batch_limit": 1000})
    recall_limit = max(k, len(sids))
    r = srv.call("perseus_vault_recall", {"query": inst["question"], "mode": "hybrid",
                                  "category": qid, "limit": recall_limit, "trust_weight": 0,
                                  "min_decay": 0, "skip_side_effects": True})
    wire = normalize_recall_response(r, limit=recall_limit)
    wire_items = wire["items"] if wire["status"] == "complete" else []
    try:
        items = stable_ranked_items(wire_items, inst["question"]) if wire_items else []
    except (TypeError, ValueError):
        wire["status"] = "unavailable"
        wire["reason"] = "malformed_recall_response"
        items = []
    ranked_ids = [it.get("key") or it.get("id") for it in items]
    pos = {sid: i + 1 for i, sid in enumerate(ranked_ids)}

    ranks = {g: pos.get(g) for g in gold}
    if shared:
        ranks[update_id] = pos.get(SHARED_FACT_KEY)
    try:
        replay_envelope, replay_snapshot = _make_replay_artifact(
            inst,
            qid,
            items,
            k,
            split=split,
            corpus_sha256=corpus_sha256 or replay_sha256_text("longmemeval-corpus-unbound"),
            config_sha256=config_sha256 or replay_sha256_text("longmemeval-config-unbound"),
            code_sha256=code_sha256 or replay_sha256_text("longmemeval-code-unbound"),
            preflight=preflight,
            status=wire["status"],
            reason=wire.get("reason"),
            runtime_binding=runtime_binding,
        )
    except (ReplayValidationError, ValueError, TypeError, KeyError):
        replay_envelope, replay_snapshot = _make_replay_artifact(
            inst,
            qid,
            [],
            k,
            split=split,
            corpus_sha256=corpus_sha256 or replay_sha256_text("longmemeval-corpus-unbound"),
            config_sha256=config_sha256 or replay_sha256_text("longmemeval-config-unbound"),
            code_sha256=code_sha256 or replay_sha256_text("longmemeval-code-unbound"),
            preflight=preflight,
            status="unavailable",
            reason="malformed_recall_response",
            runtime_binding=runtime_binding,
        )
        wire["status"] = "unavailable"
        ranks = {g: None for g in gold}
    return ranks, len(sids), update_id, replay_envelope, replay_snapshot, wire["status"]


def coverage_at(records, k):
    """Fraction of questions (with >=1 gold session) whose gold sessions are
    ALL ranked <= k."""
    scored = [
        rec for rec in records
        if rec["gold"] and recall_status_is_scoreable(rec.get("wire_status"))
    ]
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
    scored = [
        rec for rec in records
        if rec["gold"] and recall_status_is_scoreable(rec.get("wire_status"))
    ]
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


def _rank_depth_buckets(records, depth):
    """Classify evidence ranks relative to the requested retrieval depth."""
    k_recoverable = []
    hard = []
    for rec in records:
        if not recall_status_is_scoreable(rec.get("wire_status")):
            continue
        gold = list(rec.get("gold", []) or [])
        ranks = [rec.get("ranks", {}).get(evidence_id) for evidence_id in gold]
        if any(not isinstance(rank, int) or isinstance(rank, bool) or rank > depth for rank in ranks):
            hard.append(rec["question_id"])
            continue
        worst = max(ranks, default=0)
        if worst > 10:
            k_recoverable.append({
                "question_id": rec["question_id"],
                "worst_rank": worst,
                "question_type": rec.get("question_type", "unknown"),
            })
    return sorted(k_recoverable, key=lambda row: (row["worst_rank"], row["question_id"])), sorted(hard)


def _make_sufficiency_report(records, *, depth, dataset_sha256, config_sha256, code_sha256):
    """Seal gold-aware evaluator inputs, then publish only its projection."""
    evaluator_rows = []
    for record in records:
        gold = list(record.get("gold", []) or [])
        if not gold:
            continue
        ranked = None if not recall_status_is_scoreable(record.get("wire_status")) else [
            f"rank-slot-{index + 1}" for index in range(depth)
        ]
        if ranked is not None:
            for evidence_id, rank in record["ranks"].items():
                if isinstance(rank, int) and 1 <= rank <= depth:
                    ranked[rank - 1] = evidence_id
        latest = [record["update_gold"]] if record.get("update_gold") else []
        temporal = gold if record.get("question_type") == "temporal-reasoning" else []
        stale = []
        if latest:
            stale = [evidence_id for evidence_id in gold if evidence_id != latest[0]]
        evaluator_rows.append(
            {
                "question_id": record["question_id"],
                "question_type": record.get("question_type", "unknown"),
                "required_evidence": gold,
                "latest_evidence": latest,
                "temporal_anchors": temporal,
                "stale_evidence": stale,
                "ranked_ids": ranked,
                "status": "available" if recall_status_is_scoreable(record.get("wire_status")) else "unavailable",
            }
        )
    if not evaluator_rows:
        return None
    ks = tuple(k for k in (1, 3, 5, 10, 20, 50) if k <= depth)
    return build_sufficiency_report(
        evaluator_rows,
        dataset_sha256=dataset_sha256,
        fixture_sha256=replay_sha256_text("longmemeval-sufficiency-fixture-v1"),
        retrieval_config_sha256=config_sha256,
        code_sha256=code_sha256,
        ks=ks,
        focus_strata={
            "multi-evidence": ["multi-session", "knowledge-update"],
            "temporal": ["temporal-reasoning"],
        },
    )


def _retrieval_journal_digest(record):
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return replay_sha256_text(replay_stable_json(payload))


def _seal_retrieval_journal_record(record):
    sealed = dict(record)
    sealed["record_sha256"] = _retrieval_journal_digest(sealed)
    return sealed


def _expected_update_gold(instance):
    sids = list(instance.get("haystack_session_ids", []) or [])
    dates = list(instance.get("haystack_dates", []) or [])
    by_id = {sid: date for sid, date in zip(sids, dates)}
    dated_gold = sorted(
        [gold for gold in instance.get("answer_session_ids", []) or [] if by_id.get(gold)],
        key=lambda gold: to_ms(by_id[gold]),
    )
    return dated_gold[-1] if len(dated_gold) >= 2 else None


def _retrieval_rank_for_key(envelope, key):
    candidate_id = f"candidate-{replay_sha256_text(key)}"
    candidate_digest = replay_sha256_text(candidate_id)
    for candidate in envelope.get("candidates", []):
        if candidate.get("candidate_id_sha256") == candidate_digest:
            return candidate.get("final_rank")
    return None


def validate_retrieval_resume_record(
    record,
    *,
    instance,
    depth,
    ku_shared_key=False,
    runtime_binding=None,
    allow_synthetic=False,
):
    """Validate one retrieval journal row against the current dataset instance."""
    fields = {
        "question_id", "question_type", "gold", "update_gold", "ranks", "wire_status",
        "n_haystack_sessions", "retrieval_replay", "retrieval_snapshot", "preflight",
        "record_sha256",
    }
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError("retrieval journal record fields are incomplete or unknown")
    if not isinstance(record.get("record_sha256"), str) or record["record_sha256"] != _retrieval_journal_digest(record):
        raise ValueError("retrieval journal record digest mismatch")
    if record.get("question_id") != instance.get("question_id"):
        raise ValueError("retrieval journal question_id differs from current dataset")
    if record.get("question_type") != instance.get("question_type", "unknown"):
        raise ValueError("retrieval journal question_type differs from current dataset")
    expected_gold = list(instance.get("answer_session_ids", []) or [])
    if record.get("gold") != expected_gold:
        raise ValueError("retrieval journal gold differs from current dataset")
    if record.get("update_gold") != _expected_update_gold(instance):
        raise ValueError("retrieval journal update_gold differs from current dataset")
    if record.get("n_haystack_sessions") != len(instance.get("haystack_session_ids", []) or []):
        raise ValueError("retrieval journal session count differs from current dataset")
    if not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0:
        raise ValueError("retrieval journal depth is malformed")
    status = record.get("wire_status")
    if status not in {"complete", "empty", "partial", "degraded", "unavailable"}:
        raise ValueError("retrieval journal wire status is invalid")
    ranks = record.get("ranks")
    if not isinstance(ranks, dict) or set(ranks) != set(expected_gold):
        raise ValueError("retrieval journal ranks are not bound to current gold")
    for key, rank in ranks.items():
        if rank is not None and (isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= depth):
            raise ValueError(f"retrieval journal rank is malformed for {key}")
    if not isinstance(record.get("preflight"), dict):
        raise ValueError("retrieval journal preflight is malformed")
    try:
        if runtime_binding is None and not allow_synthetic:
            raise ValueError("retrieval journal runtime binding is missing")
        if runtime_binding is not None:
            validate_recall_preflight(record["preflight"], **runtime_binding)
        else:
            validate_recall_preflight(record["preflight"])
        replay_result = validate_replay_artifact(
            record["retrieval_replay"],
            record["retrieval_snapshot"],
            runtime_binding=runtime_binding,
            allow_synthetic=allow_synthetic,
        )
    except Exception as exc:
        raise ValueError("retrieval journal replay artifact is invalid") from exc
    envelope = record["retrieval_replay"]
    if envelope.get("preflight") != record["preflight"]:
        raise ValueError("retrieval journal preflight differs from replay artifact")
    if envelope.get("request", {}).get("cell_id") != record["question_id"]:
        raise ValueError("retrieval journal replay cell differs from question")
    if envelope.get("status") != status or replay_result.get("status") != status:
        raise ValueError("retrieval journal status differs from replay artifact")
    for key in expected_gold:
        expected_rank = _retrieval_rank_for_key(envelope, key)
        if ranks[key] != expected_rank:
            if not (ku_shared_key and key in (instance.get("answer_session_ids", []) or [])
                    and record.get("update_gold") == key and ranks[key] == _retrieval_rank_for_key(envelope, SHARED_FACT_KEY)):
                raise ValueError("retrieval journal rank differs from replay artifact")


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
    ap.add_argument("--bin", default=None, help="perseus-vault binary (else auto-located / PERSEUS_VAULT_BIN)")
    ap.add_argument("--out", default=str(HERE / "diag_report.json"))
    ap.add_argument("--journal", default=None, help="Crash-safe per-question journal (resumable)")
    ap.add_argument("--resume", action="store_true", help="Resume from --journal")
    ap.add_argument("--replay-out", default=None, help="Hash-only retrieval replay JSONL (default next to --out)")
    ap.add_argument("--snapshot-out", default=None, help="Hash-only replay snapshot JSONL (default next to --out)")
    ap.add_argument("--min-coverage-at", type=parse_floor, default=None, metavar="K:FRAC",
                    help="Regression gate: exit non-zero if coverage@K < FRAC (e.g. 20:0.95)")
    args = ap.parse_args()
    # LongMemEval contains adversarial-looking text. The benchmark fixture
    # supplies the hash-bound source admission; disable only content lint so
    # those legitimate rows can reach the retrieval measurement.
    os.environ["PERSEUS_VAULT_DISABLE_ADMISSION_LINT"] = "1"
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
    db = str(Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp") / "perseus_vault-diag.db")

    def wipe():
        for ext in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(db + ext)
            except OSError:
                pass

    run_config = {"split": args.split, "n": len(data), "k": args.k,
                  "only_types": sorted(args.only_types) if args.only_types else None,
                  "ku_shared_key": args.ku_shared_key}
    preflight_by_question = {}
    corpus_sha256 = replay_sha256_text(replay_stable_json(data))
    config_sha256 = replay_sha256_text(replay_stable_json(run_config))
    code_sha256 = replay_sha256_text(
        Path(__file__).read_text(encoding="utf-8")
        + (Path(__file__).resolve().parents[1] / "package" / "common" / "replay.py").read_text(encoding="utf-8")
    )
    def make_preflight(qid: str):
        return prepare_recall_preflight(
            binary=binary,
            db_path=db,
            dataset=data,
            config={**run_config, "question_id": qid},
            repo_root=str(REPO),
        )
    replay_rows = []
    snapshot_rows = []

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
            if not lines or not isinstance(lines[0], dict) or set(lines[0]) != {"_config"}:
                sys.exit(f"error: --resume: {journal_path} has no config header")
            if lines[0]["_config"] != run_config:
                sys.exit("error: --resume config mismatch:\n"
                         f"  journal: {lines[0]['_config']}\n  current: {run_config}")
            instances_by_id = {inst["question_id"]: inst for inst in data}
            loaded = []
            seen = set()
            preflight_seen = {}
            for rec in lines[1:]:
                if not isinstance(rec, dict):
                    sys.exit("error: --resume journal contains a malformed record")
                qid = rec.get("question_id")
                instance = instances_by_id.get(qid)
                if instance is None:
                    sys.exit("error: --resume journal record is not in the current dataset")
                try:
                    runtime_binding = {
                        "binary": binary,
                        "db_path": db,
                        "repo_root": str(REPO),
                        "dataset": data,
                        "config": {**run_config, "question_id": qid},
                    }
                    validate_retrieval_resume_record(
                        rec,
                        instance=instance,
                        depth=args.k,
                        ku_shared_key=args.ku_shared_key,
                        runtime_binding=runtime_binding,
                    )
                    if qid in seen:
                        raise ValueError("duplicate retrieval journal record")
                    seen.add(qid)
                    previous = preflight_seen.get(qid)
                    if previous is not None and previous != rec["preflight"]:
                        raise ValueError("inconsistent retrieval preflight bindings")
                    if previous is None:
                        validate_recall_preflight(
                            rec["preflight"],
                            binary=binary,
                            db_path=db,
                            repo_root=str(REPO),
                            dataset=data,
                            config={**run_config, "question_id": qid},
                        )
                        preflight_seen[qid] = rec["preflight"]
                    else:
                        validate_recall_preflight(
                            rec["preflight"],
                            binary=binary,
                            db_path=db,
                            repo_root=str(REPO),
                            dataset=data,
                            config={**run_config, "question_id": qid},
                        )
                except Exception as exc:
                    sys.exit(f"error: --resume journal record failed validation: {type(exc).__name__}")
                loaded.append(rec)
            for rec in loaded:
                qid = rec["question_id"]
                done[qid] = rec
                records.append(rec)
                preflight_by_question[qid] = rec["preflight"]
                replay_rows.append(rec["retrieval_replay"])
                snapshot_rows.append({"cell_id": qid, "snapshot": rec["retrieval_snapshot"]})
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
        cell_preflight = make_preflight(qid)
        preflight_by_question[qid] = cell_preflight
        srv = PerseusVaultServer(binary, db)
        try:
            ranks, n_sess, update_id, replay_envelope, replay_snapshot, wire_status = gold_ranks(
                inst,
                srv,
                qid,
                args.k,
                ku_shared=args.ku_shared_key,
                split=args.split,
                corpus_sha256=corpus_sha256,
                config_sha256=cell_preflight["config_sha256"],
                code_sha256=code_sha256,
                preflight=cell_preflight,
                runtime_binding={
                    "binary": binary,
                    "db_path": db,
                    "repo_root": str(REPO),
                    "dataset": data,
                    "config": {**run_config, "question_id": qid},
                },
            )
        finally:
            srv.close()
        rec = {
            "question_id": qid,
            "question_type": inst.get("question_type", "unknown"),
            "gold": list(inst.get("answer_session_ids", []) or []),
            "update_gold": update_id,
            "ranks": ranks,
            "wire_status": wire_status,
            "n_haystack_sessions": n_sess,
            "retrieval_replay": replay_envelope,
            "retrieval_snapshot": replay_snapshot,
            "preflight": cell_preflight,
        }
        replay_rows.append(replay_envelope)
        snapshot_rows.append({"cell_id": qid, "snapshot": replay_snapshot})
        if journal:
            sealed_record = _seal_retrieval_journal_record(rec)
            records.append(sealed_record)
            journal.write(json.dumps(sealed_record) + "\n")
            journal.flush()
        else:
            records.append(rec)
        if (idx + 1) % 25 == 0:
            print(f"  {idx + 1}/{total} …", file=sys.stderr)

    if journal:
        journal.close()

    # ── coverage ladder + miss buckets ──────────────────────────────────────
    scored = [
        r for r in records
        if r["gold"] and recall_status_is_scoreable(r.get("wire_status"))
    ]
    coverage = {f"@{k}": coverage_at(records, k) for k in ladder}
    coverage_latest = {f"@{k}": coverage_latest_at(records, k) for k in ladder}
    sufficiency_report = _make_sufficiency_report(
        records,
        depth=args.k,
        dataset_sha256=corpus_sha256,
        config_sha256=config_sha256,
        code_sha256=code_sha256,
    )

    k_recoverable, hard = _rank_depth_buckets(scored, args.k)
    preflight_report = {"questions": {key: preflight_by_question[key] for key in sorted(preflight_by_question)}}

    report = {
        "benchmark": "perseus-vault-longmemeval-retrieval-coverage",
        "metric": "gold-evidence coverage@k (offline, judge-free)",
        "dataset": data_path.name,
        "split": f"longmemeval_{args.split}",
        "split_size": split_size,
        "n_instances": total,
        "n_scored": len(scored),
        "n_unavailable": sum(1 for record in records if not recall_status_is_scoreable(record.get("wire_status"))),
        "retrieval": {"mode": "hybrid", "k": args.k, "trust_weight": 0, "min_decay": 0},
        "ingest_shape": "ku-shared-key (product)" if args.ku_shared_key else "unique-key-per-session (benchmark)",
        "coverage_at_k": coverage,
        "coverage_latest_at_k": coverage_latest,
        "sufficiency": sufficiency_report,
        "k_recoverable": sorted(k_recoverable, key=lambda x: x["worst_rank"]),
        "hard_misses": sorted(hard),
        "binary": Path(binary).name,
        "preflight": preflight_report,
        "response_schema": next(iter(preflight_by_question.values()), {}).get("response_schema"),
        "platform": platform.platform(),
        "offline": True,
    }
    sig = hashlib.sha256(json.dumps({
        "coverage": coverage, "coverage_latest": coverage_latest, "hard": sorted(hard),
        "n": total, "n_scored": len(scored),
        "n_unavailable": sum(1 for record in records if not recall_status_is_scoreable(record.get("wire_status"))),
        "k": args.k, "ku_shared_key": args.ku_shared_key,
        "sufficiency_signature": sufficiency_report["signature_sha256"] if sufficiency_report else None,
        "preflight": preflight_report,
    }, sort_keys=True).encode("utf-8")).hexdigest()
    report["signature_sha256"] = sig
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    replay_path = Path(args.replay_out) if args.replay_out else out_path.with_name(out_path.stem + "_replay.jsonl")
    snapshot_path = Path(args.snapshot_out) if args.snapshot_out else out_path.with_name(out_path.stem + "_snapshot.jsonl")
    replay_rows.sort(key=lambda item: item["request"]["cell_id"])
    snapshot_rows.sort(key=lambda item: item["cell_id"])
    replay_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in replay_rows), encoding="utf-8")
    snapshot_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in snapshot_rows), encoding="utf-8")

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
