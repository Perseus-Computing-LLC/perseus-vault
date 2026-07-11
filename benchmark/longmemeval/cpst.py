#!/usr/bin/env python3
"""cpst.py — Cost Per Successfully completed Task, from signed qa.py reports.

Fuses cost and quality into the one number that survives skeptics: total
answerer cost divided by tasks answered correctly. A system that is cheaper
because it is worse gets a WORSE CPST, so "cheaper but broken" cannot win.

Accounting rules (deliberate, stated so they can be argued with):
  * Cost = ANSWERER tokens only, priced per model. The judge is measurement
    apparatus, not production cost — its spend is shown separately as
    "harness overhead" and never enters CPST.
  * Tokens = API-billed usage recorded in the report (`api_usage_tokens` /
    per-question `ans_usage`). If a system has no billed usage (mock, dry-run,
    or a pre-usage-patch run), it is EXCLUDED from the CPST table and listed
    as not-quotable — estimates are for planning, not publication.
  * Accuracy denominator = graded questions only (answer/judge errors are
    excluded by qa.py and never counted as wrong).
  * Perseus overhead: retrieval + embeddings run locally (bundled ONNX, no
    API), so no per-call API cost exists to add. Local compute is disclosed,
    not priced.

Usage:
  # single report containing several systems
  python cpst.py --reports qa_report.json

  # merge systems across reports, slicing every system to the same subset
  python cpst.py --reports qa_report.json report.json \
      --manifest longmemeval_s_subset100.manifest.json

Output: a markdown table + machine-readable cpst.json next to the first report.
"""
import argparse
import json
from pathlib import Path

from qa import PRICING, FALLBACK_PRICE   # same pinned price table as the harness


def price_for(model):
    return PRICING.get(model, FALLBACK_PRICE)


def collect(report, qids=None):
    """Per-system slice of a qa_report: verdicts, accuracy, billed usage."""
    out = {}
    model = report.get("answerer_model", "unknown")
    for v in report.get("per_question", []):
        if qids is not None and v["question_id"] not in qids:
            continue
        s = out.setdefault(v["system"], {"model": model, "n": 0, "correct": 0,
                                         "ans_prompt": 0, "ans_completion": 0,
                                         "judge_prompt": 0, "judge_completion": 0,
                                         "missing_usage": 0})
        if v.get("error") is not None:
            continue
        s["n"] += 1
        s["correct"] += int(v["correct"])
        au, ju = v.get("ans_usage"), v.get("judge_usage")
        if au:
            s["ans_prompt"] += au.get("prompt_tokens", 0)
            s["ans_completion"] += au.get("completion_tokens", 0)
        else:
            s["missing_usage"] += 1
        if ju:
            s["judge_prompt"] += ju.get("prompt_tokens", 0)
            s["judge_completion"] += ju.get("completion_tokens", 0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", nargs="+", required=True, help="qa_report.json file(s)")
    ap.add_argument("--manifest", default=None,
                    help="Subset manifest: every system is sliced to its question_ids")
    ap.add_argument("--out", default=None, help="Output JSON (default: cpst.json next to first report)")
    args = ap.parse_args()

    qids = None
    if args.manifest:
        qids = set(json.loads(Path(args.manifest).read_text(encoding="utf-8"))["question_ids"])

    systems, provenance = {}, []
    for rp in args.reports:
        report = json.loads(Path(rp).read_text(encoding="utf-8"))
        provenance.append({"file": Path(rp).name,
                           "signature_sha256": report.get("signature_sha256"),
                           "answerer_model": report.get("answerer_model"),
                           "judge_model": report.get("judge_model"),
                           "commit": report.get("commit")})
        for name, s in collect(report, qids).items():
            if name in systems:
                raise SystemExit(f"system '{name}' appears in more than one report — "
                                 "slice inputs so each system has one source")
            systems[name] = s

    rows, excluded = [], []
    for name, s in sorted(systems.items()):
        if s["n"] == 0:
            continue
        billed = s["ans_prompt"] + s["ans_completion"]
        if billed == 0 or s["missing_usage"] == s["n"]:
            excluded.append((name, "no API-billed usage recorded — not quotable"))
            continue
        pin, pout = price_for(s["model"])
        cost = s["ans_prompt"] / 1e6 * pin + s["ans_completion"] / 1e6 * pout
        jcost = s["judge_prompt"] / 1e6 * pin + s["judge_completion"] / 1e6 * pout
        acc = s["correct"] / s["n"]
        rows.append({
            "system": name, "model": s["model"], "n": s["n"],
            "accuracy": round(acc, 4),
            "answer_prompt_tokens": s["ans_prompt"],
            "answer_completion_tokens": s["ans_completion"],
            "avg_prompt_tokens_per_q": round(s["ans_prompt"] / s["n"]),
            "answer_cost_usd": round(cost, 4),
            "cpst_usd": round(cost / s["correct"], 4) if s["correct"] else None,
            "judge_cost_usd_overhead": round(jcost, 4),
            "partial_usage_questions": s["missing_usage"],
        })

    base = next((r for r in rows if r["system"] == "fullcontext"), None)
    md = ["| system | n | accuracy | avg prompt tok/q | answer cost | **CPST** | vs fullcontext |",
          "|---|---:|---:|---:|---:|---:|---|"]
    for r in rows:
        rel = ""
        if base and r["system"] != "fullcontext" and r["cpst_usd"] and base["cpst_usd"]:
            rel = (f"{base['cpst_usd'] / r['cpst_usd']:.1f}× cheaper per correct answer, "
                   f"{base['avg_prompt_tokens_per_q'] / max(1, r['avg_prompt_tokens_per_q']):.1f}× fewer tokens")
        md.append(f"| {r['system']} | {r['n']} | {r['accuracy'] * 100:.1f}% "
                  f"| {r['avg_prompt_tokens_per_q']:,} | ${r['answer_cost_usd']:.2f} "
                  f"| **${r['cpst_usd']}** | {rel} |")
    table = "\n".join(md)

    result = {"metric": "CPST (answerer USD per correctly answered question)",
              "subset": Path(args.manifest).name if args.manifest else None,
              "n_questions": qids and len(qids),
              "rows": rows, "excluded": excluded, "provenance": provenance,
              "accounting": "answerer tokens only, API-billed; judge shown as overhead; "
                            "retrieval/embeddings are local (no API cost)"}
    out_path = Path(args.out or Path(args.reports[0]).parent / "cpst.json")
    out_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(table)
    for name, why in excluded:
        print(f"\nEXCLUDED {name}: {why}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
