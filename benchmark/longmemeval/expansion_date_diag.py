#!/usr/bin/env python3
"""#588 date-aware arm demonstrator (free, offline).

`retrieval_diag.py` can't exercise the date-aware arm because it never passes a
query-date anchor, so `PERSEUS_VAULT_DATE_ARM` no-ops there. This companion sets
`PERSEUS_VAULT_QUERY_DATE` to each instance's `question_date` before spawning the
per-instance server, so the engine can resolve relative-date expressions ("two
weeks ago") against it. It reports each gold session's rank OFF vs ON, exactly
like `cov_by_type.py --track`, for the date-keyed question(s).

    python expansion_date_diag.py --data longmemeval_s_cleaned.json \
        --bin ../../target/release/perseus-vault.exe --ids gpt4_1e4a8aec --k 20
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from run import PerseusVaultServer, session_text, find_binary  # noqa: E402
from benchmark.package.common.replay import (
    normalize_recall_response,
    prepare_recall_preflight,
    recall_status_is_scoreable,
)  # noqa: E402


def session_note(date, turns):
    prefix = f"session date: {date}\n" if date else ""
    return prefix + session_text(turns)


def ranks_for(inst, binary, db, k, env, *, preflight_config):
    for key, val in env.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    preflight = prepare_recall_preflight(
        binary=binary,
        db_path=db,
        dataset=inst,
        config=preflight_config,
        repo_root=str(REPO),
    )
    sids = inst["haystack_session_ids"]
    dates = inst.get("haystack_dates") or [None] * len(sids)
    srv = PerseusVaultServer(binary, db)
    try:
        for sid, turns, d in zip(sids, inst["haystack_sessions"], dates):
            srv.call("perseus_vault_remember", {"category": inst["question_id"], "key": sid,
                                        "body_json": json.dumps({"note": session_note(d, turns)}),
                                        "type": "fact"})
        srv.call("perseus_vault_embed", {"batch_category": inst["question_id"], "batch_limit": 1000})
        r = srv.call("perseus_vault_recall", {"query": inst["question"], "mode": "hybrid",
                                      "category": inst["question_id"], "limit": k,
                                      "trust_weight": 0, "min_decay": 0})
    finally:
        srv.close()
    wire = normalize_recall_response(r, limit=k)
    if not recall_status_is_scoreable(wire["status"]):
        raise RuntimeError("recall unavailable: malformed, partial, or degraded wire response")
    items = wire["items"]
    pos = {sid: i + 1 for i, sid in enumerate(it.get("key") or it.get("id") for it in items)}
    return {g: pos.get(g) for g in inst.get("answer_session_ids", [])}, preflight


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--bin", default=None)
    ap.add_argument("--ids", nargs="+", required=True)
    ap.add_argument("--k", type=int, default=20)
    args = ap.parse_args()
    binary = find_binary(args.bin)
    db = str(Path(os.environ.get("TEMP") or "/tmp") / "perseus_vault-datearm.db")
    data = {i["question_id"]: i for i in json.loads(Path(args.data).read_text(encoding="utf-8"))}

    OFF = {"PERSEUS_VAULT_QUERY_EXPANSION": None, "PERSEUS_VAULT_DATE_ARM": None,
           "PERSEUS_VAULT_QUERY_DATE": None}
    for qid in args.ids:
        inst = data[qid]
        off, off_preflight = ranks_for(
            inst,
            binary,
            db,
            args.k,
            OFF,
            preflight_config={"question_id": qid, "arm": "off", "k": args.k},
        )
        on, on_preflight = ranks_for(
            inst,
            binary,
            db,
            args.k,
            {
                "PERSEUS_VAULT_QUERY_EXPANSION": "1",
                "PERSEUS_VAULT_DATE_ARM": "1",
                "PERSEUS_VAULT_QUERY_DATE": inst.get("question_date", ""),
            },
            preflight_config={"question_id": qid, "arm": "on", "k": args.k},
        )
        print(f"\n== {qid} [{inst.get('question_type')}] qdate={inst.get('question_date')}")
        print(f"   Q: {inst['question'][:90]}")
        print(f"   preflight: off={off_preflight['database_id_sha256'][:16]}... "
              f"on={on_preflight['database_id_sha256'][:16]}...")
        for g in inst.get("answer_session_ids", []):
            print(f"   {g}: OFF rank {off.get(g)} -> ON rank {on.get(g)}")


if __name__ == "__main__":
    main()
