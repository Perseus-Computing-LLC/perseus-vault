#!/usr/bin/env python3
"""Perseus Vault depth-sweep harness (#954).

Measures the recall-vs-depth and recall-vs-token-budget curves over the
fixed offline dataset, so operators pick a per-deployment operating point
instead of assuming "deeper = better". Non-monotonicity and saturation are
expected, documented properties of the retrieval contract
(docs/specs/recall-serving-contract.md §2) — this harness makes the curve
visible and reproducible.

Sweeps:
  - depth: `k` (limit) in {8, 16, 24, 32, 48, 64, 96, 128} on the hybrid
    (RRF) serving path; reports recall@k and the injected tokens.
  - budget: fused-mode token budgets in {1024, 2048, 4096, 8192, 16384};
    reports recall@k and tokens actually delivered (from the truncation
    trace).

Usage:
    python depth_sweep.py                        # auto-locate binary
    python depth_sweep.py --bin /path/to/perseus-vault
    python depth_sweep.py --dataset other.json --out depth_sweep_report.json

Fully offline and deterministic (hybrid was made byte-stable in #247); the
report signature covers the hybrid sweep.
"""
import argparse
import hashlib
import json
import os
import platform
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from run import PerseusVault, find_binary, score  # noqa: E402
from benchmark.package.common.replay import finalize_recall_preflight, normalize_recall_response, prepare_recall_preflight  # noqa: E402

DEPTH_KS = [8, 16, 24, 32, 48, 64, 96, 128]
BUDGETS = [1024, 2048, 4096, 8192, 16384]
EST = 4  # estimated tokens per char (chars/4, the #883 contract)


def item_tokens(item: dict) -> int:
    """Estimated injected tokens for one recall item (chars/4)."""
    body = item.get("body_json")
    if body is None:
        body = item.get("body", "")
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if not isinstance(body, str):
        body = str(body)
    return max(1, len(body) // EST)


def main():
    ap = argparse.ArgumentParser(description="Perseus Vault depth-sweep harness (#954)")
    ap.add_argument("--bin", default=None)
    ap.add_argument("--dataset", default=str(HERE / "dataset.json"))
    ap.add_argument("--out", default=str(HERE / "depth_sweep_report.json"))
    ap.add_argument("--ks", nargs="+", type=int, default=DEPTH_KS)
    ap.add_argument("--budgets", nargs="+", type=int, default=BUDGETS)
    args = ap.parse_args()

    binary = find_binary(args.bin)
    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    memories, queries = data["memories"], data["queries"]
    ks = sorted(set(args.ks))
    budgets = sorted(set(args.budgets))

    db_dir = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    db = str(db_dir / "perseus_vault-depth-sweep.db")
    preflight = prepare_recall_preflight(
        binary=binary,
        db_path=db,
        dataset=data,
        config={"ks": ks, "budgets": budgets},
        repo_root=str(REPO),
    )

    m = PerseusVault(binary, db)

    print(f"Ingesting {len(memories)} memories...", flush=True)
    for mem in memories:
        m.call("perseus_vault_remember", {
            "category": mem["category"], "key": mem["key"],
            "body_json": json.dumps({"note": mem["note"]}), "type": "fact",
        })
    cats = sorted({mem["category"] for mem in memories})
    embedded = 0
    for cat in cats:
        rep = m.call("perseus_vault_embed", {"batch_category": cat, "batch_limit": 1000})
        if isinstance(rep, dict):
            embedded += int(rep.get("embedded", 0) or 0)
    print(f"Embedded {embedded} entities (bundled ONNX).", flush=True)

    # 1. Depth sweep (hybrid serving path).
    depth_rows = []
    for k in ks:
        hits_total = 0
        tokens_total = 0
        for q in queries:
            r = m.call("perseus_vault_recall", {
                "query": q["q"], "mode": "hybrid", "limit": k,
                "trust_weight": 0, "min_decay": 0,
            })
            wire = normalize_recall_response(r, limit=k)
            if wire["status"] != "complete":
                raise RuntimeError(f"recall unavailable or incomplete: {wire['status']}")
            items = wire["items"]
            ranked = [it.get("key") or it.get("id") for it in items]
            s = score(ranked, q["relevant"], [k])
            hits_total += s[f"recall@{k}"]
            tokens_total += sum(item_tokens(it) for it in items)
        n = len(queries)
        depth_rows.append({
            "k": k,
            "recall_at_k": round(hits_total / n, 4),
            "tokens_delivered": round(tokens_total / n, 1),
            "tokens_per_recall_point": round(tokens_total / max(hits_total, 1), 1),
        })

    # 2. Budget sweep (fused serving path with token-budget truncation).
    budget_rows = []
    for budget in budgets:
        hits_total = 0
        tokens_total = 0
        for q in queries:
            r = m.call("perseus_vault_recall", {
                "query": q["q"], "mode": "fused", "limit": max(ks),
                "max_tokens": budget, "trust_weight": 0, "min_decay": 0,
            })
            wire = normalize_recall_response(r, limit=max(ks))
            if wire["status"] != "complete":
                raise RuntimeError(f"recall unavailable or incomplete: {wire['status']}")
            items = wire["items"]
            ranked = [it.get("key") or it.get("id") for it in items]
            s = score(ranked, q["relevant"], [max(ks)])
            hits_total += s[f"recall@{max(ks)}"]
            tokens_total += sum(item_tokens(it) for it in items)
        n = len(queries)
        budget_rows.append({
            "budget_tokens": budget,
            "recall_at_k": round(hits_total / n, 4),
            "tokens_delivered": round(tokens_total / n, 1),
        })

    preflight = finalize_recall_preflight(preflight, db_path=db)

    # Signature over the deterministic hybrid depth sweep.
    sig_payload = json.dumps({
        "dataset": data.get("name"), "ks": ks, "depth_rows": depth_rows,
        "budget_rows": budget_rows, "preflight": preflight,
    }, sort_keys=True)
    signature = hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()

    report = {
        "benchmark": "perseus_vault-depth-sweep",
        "issue": "#954",
        "dataset": data.get("name"),
        "n_memories": len(memories),
        "n_queries": len(queries),
        "depth_sweep": depth_rows,
        "budget_sweep": budget_rows,
        "binary": Path(binary).name,
        "preflight": preflight,
        "response_schema": preflight["response_schema"],
        "platform": platform.platform(),
        "offline": True,
        "embedding": {"source": "bundled-onnx", "embedded": embedded},
        "signature_sha256": signature,
        "signature_covers": ["hybrid-depth-sweep"],
        "note": ("recall-vs-depth is non-monotonic and saturates; pick the "
                 "operating point from these curves, not convention "
                 "(docs/specs/recall-serving-contract.md §2)."),
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"\nPerseus Vault depth sweep - {data.get('name')} ({len(queries)} queries)")
    print(f"{'k':>5}  {'recall@k':>9}  {'tokens':>8}  {'tokens/recall':>14}")
    for row in depth_rows:
        print(f"{row['k']:>5}  {row['recall_at_k']*100:>8.1f}%  {row['tokens_delivered']:>8.1f}"
              f"  {row['tokens_per_recall_point']:>14.1f}")
    print(f"\n{'budget':>7}  {'recall@k':>9}  {'tokens':>8}")
    for row in budget_rows:
        print(f"{row['budget_tokens']:>7}  {row['recall_at_k']*100:>8.1f}%  {row['tokens_delivered']:>8.1f}")
    best = min(depth_rows, key=lambda r: (r["tokens_per_recall_point"], -r["recall_at_k"]))
    print(f"\noperating point suggestion: k={best['k']} "
          f"(recall {best['recall_at_k']*100:.1f}%, {best['tokens_delivered']:.0f} tokens/query)")
    print(f"signature: {signature[:16]}...  ->  {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
