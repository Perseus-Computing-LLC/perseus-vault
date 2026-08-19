#!/usr/bin/env python3
"""Perseus Vault offline recall-quality benchmark.

Measures whether Perseus Vault *retrieves the right memory*, not how fast it does so
(the latency/throughput suite lives in ../run.py). It is fully offline and
deterministic: it drives the real `perseus_vault` binary over MCP stdio, ingests a
paraphrase-heavy dataset, populates dense vectors with the **bundled** ONNX
embedding model (no network, no API key, no LLM), and scores recall@k / MRR for
each search mode (fts5 keyword, dense vector, hybrid RRF).

Usage:
    python run.py                       # auto-locate the binary, score, write report.json
    python run.py --bin /path/to/perseus-vault  # explicit binary
    python run.py --dataset other.json --k 1 3 5 --out report.json
    PERSEUS_VAULT_BIN=/path/to/perseus-vault python run.py

Plugging in the real benchmarks: pass --dataset pointing at a JSON file with the
same shape ({"memories": [...], "queries": [{"q", "relevant": [keys]}]}) built
from LOCOMO or LongMemEval. The harness is dataset-agnostic.
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

from benchmark.package.common.replay import (
    build_envelope as build_replay_envelope,
    build_snapshot as build_replay_snapshot,
    sha256_text as replay_sha256_text,
    stable_json as replay_stable_json,
)
HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def find_binary(explicit: "str | None") -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("PERSEUS_VAULT_BIN"):
        candidates.append(os.environ["PERSEUS_VAULT_BIN"])
    exe = "perseus-vault.exe" if os.name == "nt" else "perseus-vault"
    candidates += [str(REPO / "target" / "release" / exe),
                   str(REPO / "target" / "debug" / exe)]
    for c in candidates:
        if c and Path(c).exists():
            return str(Path(c).resolve())
    sys.exit("error: perseus-vault binary not found. Build it (`cargo build --release`) "
             "or pass --bin / set PERSEUS_VAULT_BIN.")


class PerseusVault:
    """One MCP tools/call per process (matches the sibling perf harness).

    State persists because every call points at the same --db file.
    """

    def __init__(self, binary: str, db: str, env: "dict | None" = None):
        self.binary = binary
        self.db = db
        self.env = env

    def call(self, name: str, args: dict):
        p = subprocess.Popen([self.binary, "--db", self.db],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, env=self.env)
        w = p.stdin.write
        w(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "recall-bench", "version": "1"}}}) + "\n")
        p.stdin.flush()
        p.stdout.readline()
        w(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        w(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": name, "arguments": args}}) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        p.stdin.close()
        p.wait(timeout=120)
        resp = json.loads(line)
        r = resp.get("result", {})
        if isinstance(r, dict) and "content" in r:
            try:
                return json.loads(r["content"][0]["text"])
            except Exception:
                return r["content"][0]["text"]
        return resp


def score(ranked_keys, relevant, ks):
    """recall@k for each k, plus reciprocal rank (0 if no hit)."""
    rel = set(relevant)
    out = {f"recall@{k}": (1.0 if rel & set(ranked_keys[:k]) else 0.0) for k in ks}
    rr = 0.0
    for i, key in enumerate(ranked_keys, start=1):
        if key in rel:
            rr = 1.0 / i
            break
    out["rr"] = rr
    return out


def _recall_replay_rows(items):
    rows = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or item.get("id") or f"wire-{index + 1}")
        identity = replay_sha256_text(key)
        body = item.get("body_json", item.get("body", item.get("content", key)))
        row = {
            "candidate_id": f"candidate-{identity}",
            "source_ref": f"source-{identity}",
            "content": replay_stable_json({"candidate": key, "body": body}),
            "provenance": "vault-recall",
            "wire_rank": index + 1,
            "original_position": index + 1,
        }
        score = item.get("score")
        semantics = "provider-score-v1"
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            score = item.get("decay_score")
            semantics = "decay-score-v1"
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            row["score"] = score
            row["score_semantics"] = semantics
        rows.append(row)
    return rows


def _make_recall_replay(*, dataset_name, query, mode, limit, items, corpus_sha256, config_sha256, code_sha256):
    rows = _recall_replay_rows(items)
    snapshot = build_replay_snapshot(rows)
    cell_id = f"cell-{replay_sha256_text(replay_stable_json({'query_sha256': replay_sha256_text(query), 'mode': mode}))[:32]}"
    envelope = build_replay_envelope(
        workspace_id=f"recall:{dataset_name or 'dataset'}",
        scope="dataset:all",
        fixture_id="recall-quality-v1",
        corpus_sha256=corpus_sha256,
        retrieval_profile=f"recall:{mode}",
        mode=mode,
        top_k=limit,
        cell_id=cell_id,
        request_sha256=replay_sha256_text(query),
        config_sha256=config_sha256,
        code_sha256=code_sha256,
        context_policy="wire-order-v1",
        context_policy_version="1",
        snapshot=snapshot,
        candidates=rows,
        sequence_policy="wire_v1",
    )
    return envelope, snapshot


def main():
    ap = argparse.ArgumentParser(description="Perseus Vault offline recall-quality benchmark")
    ap.add_argument("--bin", default=None, help="Path to the perseus_vault binary")
    ap.add_argument("--dataset", default=str(HERE / "dataset.json"))
    ap.add_argument("--k", nargs="+", type=int, default=[1, 3, 5])
    ap.add_argument("--modes", nargs="+", default=["fts5", "dense", "hybrid"])
    ap.add_argument("--out", default=str(HERE / "report.json"))
    ap.add_argument("--replay-out", default=None, help="Hash-only retrieval replay JSONL (default next to --out)")
    ap.add_argument("--snapshot-out", default=None, help="Hash-only replay snapshot JSONL (default next to --out)")
    ap.add_argument("--limit", type=int, default=10, help="Results requested per query")
    ap.add_argument("--hints", action="store_true",
                    help="#919: ingest prospective query hints from memories[].hints "
                         "(server runs with PERSEUS_VAULT_HINTS_ENABLED=1) and record "
                         "the gate in the report. Run once with and once without and "
                         "compare fts5 recall to measure the vocabulary-gap delta.")
    args = ap.parse_args()

    binary = find_binary(args.bin)
    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    memories, queries = data["memories"], data["queries"]
    ks = sorted(set(args.k))
    corpus_sha256 = replay_sha256_text(replay_stable_json(data))
    config_sha256 = replay_sha256_text(replay_stable_json({"k": ks, "modes": args.modes, "limit": args.limit, "hints": args.hints}))
    code_sha256 = replay_sha256_text(Path(__file__).read_text(encoding="utf-8"))

    db_dir = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    db = str(db_dir / "perseus_vault-recall-bench.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass

    m = PerseusVault(binary, db, env=(dict(os.environ, PERSEUS_VAULT_HINTS_ENABLED="1")
                                      if args.hints else None))

    # 1. Ingest.
    print(f"Ingesting {len(memories)} memories...", flush=True)
    for mem in memories:
        remember_args = {
            "category": mem["category"], "key": mem["key"],
            "body_json": json.dumps({"note": mem["note"]}), "type": "fact",
        }
        if args.hints and mem.get("hints"):
            remember_args["hints"] = mem["hints"]
        m.call("perseus_vault_remember", remember_args)

    # 2. Populate dense vectors with the bundled local model (no network).
    cats = sorted({mem["category"] for mem in memories})
    embedded, dims = 0, None
    for cat in cats:
        rep = m.call("perseus_vault_embed", {"batch_category": cat, "batch_limit": 1000})
        if isinstance(rep, dict):
            embedded += int(rep.get("embedded", 0) or 0)
            dims = rep.get("dimensions", dims)
    dim_note = f", {dims}-dim" if dims else ""
    print(f"Embedded {embedded} entities (bundled ONNX{dim_note}).", flush=True)

    # 3. Query each mode and score.
    agg = {mode: {f"recall@{k}": 0.0 for k in ks} | {"mrr": 0.0} for mode in args.modes}
    per_query = []
    replay_rows = []
    snapshot_rows = []
    for q in queries:
        row = {"q": q["q"], "relevant": q["relevant"], "modes": {}}
        for mode in args.modes:
            r = m.call("perseus_vault_recall", {"query": q["q"], "mode": mode, "limit": args.limit,
                                        "trust_weight": 0, "min_decay": 0})
            items = r.get("items", []) if isinstance(r, dict) else []
            replay_envelope, replay_snapshot = _make_recall_replay(
                dataset_name=data.get("name"),
                query=q["q"],
                mode=mode,
                limit=args.limit,
                items=items,
                corpus_sha256=corpus_sha256,
                config_sha256=config_sha256,
                code_sha256=code_sha256,
            )
            replay_rows.append(replay_envelope)
            snapshot_rows.append({"cell_id": replay_envelope["request"]["cell_id"], "snapshot": replay_snapshot})
            ranked = [it.get("key") for it in items]
            s = score(ranked, q["relevant"], ks)
            row["modes"][mode] = {"top": ranked[:max(ks)], **s}
            for k in ks:
                agg[mode][f"recall@{k}"] += s[f"recall@{k}"]
            agg[mode]["mrr"] += s["rr"]
        per_query.append(row)

    n = len(queries)
    for mode in args.modes:
        for key in agg[mode]:
            agg[mode][key] = round(agg[mode][key] / n, 4)

    # Signature over the reproducible modes. All three modes are now
    # deterministic run-to-run: fts5 and dense always were, and `hybrid` (RRF)
    # was made byte-stable in #247 via a deterministic id tie-break in the fusion
    # plus a read-only, BM25-ranked keyword arm that no longer depends on
    # wall-clock decay or on perseus_vault_recall's access side-effects. The set below is
    # kept (empty) so a future non-reproducible mode can be excluded without
    # reworking the harness. See README.
    NONDETERMINISTIC = set()
    repro_modes = [m for m in args.modes if m not in NONDETERMINISTIC]
    sig_payload = json.dumps({
        "dataset": data.get("name"), "k": ks, "modes": repro_modes,
        "hints": args.hints,
        "metrics": {m: agg[m] for m in repro_modes},
    }, sort_keys=True)
    signature = hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()

    report = {
        "benchmark": "perseus_vault-recall-quality",
        "dataset": data.get("name"),
        "n_memories": len(memories),
        "n_queries": n,
        "k": ks,
        "modes": args.modes,
        "hints_enabled": args.hints,
        "metrics": agg,
        "binary": Path(binary).name,
        "platform": platform.platform(),
        "offline": True,
        "embedding": {"source": "bundled-onnx", "embedded": embedded, "dimensions": dims},
        "signature_sha256": signature,
        "signature_covers": repro_modes,
        "nondeterministic_modes": sorted(NONDETERMINISTIC & set(args.modes)),
        "per_query": per_query,
    }
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    replay_path = Path(args.replay_out) if args.replay_out else out_path.with_name(out_path.stem + "_replay.jsonl")
    snapshot_path = Path(args.snapshot_out) if args.snapshot_out else out_path.with_name(out_path.stem + "_snapshot.jsonl")
    replay_rows.sort(key=lambda item: item["request"]["cell_id"])
    snapshot_rows.sort(key=lambda item: item["cell_id"])
    replay_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in replay_rows), encoding="utf-8")
    snapshot_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in snapshot_rows), encoding="utf-8")

    # Human summary.
    print(f"\nPerseus Vault recall quality - {data.get('name')} ({n} queries, {len(memories)} memories)")
    hdr = "mode    " + "".join(f"  R@{k:<5}" for k in ks) + "  MRR"
    print(hdr)
    print("-" * len(hdr))
    for mode in args.modes:
        cells = "".join(f"  {agg[mode][f'recall@{k}']*100:5.1f}" for k in ks)
        print(f"{mode:<7}{cells}  {agg[mode]['mrr']:.3f}")
    print(f"\nsignature: {signature[:16]}...  ->  {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
