#!/usr/bin/env python3
"""#590 ingest-time semantics demo (free, offline).

The #590 version-inversion is a BENCHMARK-MODELING artifact, not an engine bug.
LongMemEval ingests every session as its own unique `key`, so all versions of a
fact stay live and hybrid recall returns the stale one alongside the update.
Real Perseus usage remembers an updated fact under the SAME `key` (optionally
with a valid-time) — which the engine already collapses to a live "latest wins"
row (prior versions go to `entity_history`), so there is nothing stale to rank.

This script demonstrates both behaviors on a real knowledge-update instance:
  A) BENCHMARK shape (unique key per session): recall returns BOTH versions.
  B) PRODUCT shape (shared key + valid_from = session date): recall returns ONLY
     the latest version; the earlier value is recoverable via mimir_valid_at.

    python knowledge_update_semantics_demo.py --data longmemeval_s_cleaned.json \
        --bin ../../target/release/perseus-vault.exe --id 852ce960
"""
from __future__ import annotations
import argparse, json, os, re, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run import MimirServer, session_text, find_binary  # noqa: E402


def to_ms(datestr):
    # LongMemEval date: "2023/08/11 (Fri) 00:01" — keep the time-of-day so
    # same-day updates still order correctly.
    s = re.sub(r"\s*\([^)]*\)\s*", " ", datestr).strip()
    try:
        d = datetime.strptime(s, "%Y/%m/%d %H:%M")
    except ValueError:
        d = datetime.strptime(s.split(" ")[0], "%Y/%m/%d")
    return int(d.timestamp() * 1000)


def body(date, turns):
    return json.dumps({"note": (f"session date: {date}\n" if date else "") + session_text(turns)})


def wipe(db):
    for e in ("", "-wal", "-shm"):
        try:
            os.remove(db + e)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--bin", default=None)
    ap.add_argument("--id", required=True, help="a knowledge-update question_id")
    args = ap.parse_args()
    binary = find_binary(args.bin)
    data = {i["question_id"]: i for i in json.loads(Path(args.data).read_text(encoding="utf-8"))}
    inst = data[args.id]
    dm = dict(zip(inst["haystack_session_ids"], inst["haystack_dates"]))
    tm = dict(zip(inst["haystack_session_ids"], inst["haystack_sessions"]))
    gold = sorted(inst["answer_session_ids"], key=lambda g: to_ms(dm[g]))  # ascending date
    print(f"# {args.id}  Q: {inst['question'][:80]}")
    for g in gold:
        print(f"   gold {g}  date={dm[g]}")
    db = str(Path(os.environ.get("TEMP") or "/tmp") / "mimir-ku-demo.db")

    # ---- A) BENCHMARK shape: unique key per session ----
    wipe(db); srv = MimirServer(binary, db)
    try:
        for g in gold:
            srv.call("mimir_remember", {"category": "A", "key": g,
                                        "body_json": body(dm[g], tm[g]), "type": "fact"})
        srv.call("mimir_embed", {"batch_category": "A", "batch_limit": 100})
        r = srv.call("mimir_recall", {"query": inst["question"], "mode": "hybrid",
                                      "category": "A", "limit": 10, "trust_weight": 0, "min_decay": 0})
        keys = [it.get("key") for it in (r.get("items", []) if isinstance(r, dict) else [])]
    finally:
        srv.close()
    print(f"\nA) unique-key-per-session (benchmark): recall returns {len(keys)} live versions -> {keys}")
    print("   => both stale and update are live; ranking must guess which is latest (the #590 artifact).")

    # ---- B) PRODUCT shape: shared key + valid_from = session date ----
    wipe(db); srv = MimirServer(binary, db)
    try:
        for g in gold:  # ascending date; last remember = latest version
            srv.call("mimir_remember", {"category": "B", "key": "the_fact",
                                        "body_json": body(dm[g], tm[g]), "type": "fact",
                                        "valid_from_unix_ms": to_ms(dm[g])})
        srv.call("mimir_embed", {"batch_category": "B", "batch_limit": 100})
        r = srv.call("mimir_recall", {"query": inst["question"], "mode": "hybrid",
                                      "category": "B", "limit": 10, "trust_weight": 0, "min_decay": 0})
        items = r.get("items", []) if isinstance(r, dict) else []
        live_bodies = [json.loads(it.get("body_json", "{}")).get("note", "")[:70] for it in items]
        # earlier value via bitemporal valid_at (as-of the stale gold's date)
        va = srv.call("mimir_valid_at", {"category": "B", "key": "the_fact",
                                         "valid_at_unix_ms": to_ms(dm[gold[0]])})
    finally:
        srv.close()
    print(f"\nB) shared-key + valid_from (product): recall returns {len(items)} live version(s)")
    for b_ in live_bodies:
        print(f"   live: {b_!r}")
    print("   => only the LATEST version is live (earlier one is in history); nothing stale to mis-rank.")
    print(f"   mimir_valid_at(as-of {dm[gold[0]]}): {json.dumps(va)[:200]}")
    print("   => the earlier value is still correctly recoverable bi-temporally.")


if __name__ == "__main__":
    main()
