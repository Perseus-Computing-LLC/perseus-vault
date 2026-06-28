#!/usr/bin/env python3
"""Mimir LongMemEval QA-accuracy harness (answer generation; official judge).

This is the SECOND LongMemEval stage. The first stage (retrieval) lives in
`run.py` and is offline/judge-free. This stage feeds context to an LLM, gets an
answer, and is graded by LongMemEval's OWN judge (evaluate_qa.py). We deliberately
do NOT invent a judge: that is what made the deprecated benchmarks/LONG_MEM_EVAL.md
non-credible.

Design for credibility:
  * Generates a hypotheses file in LongMemEval's exact format (jsonl of
    {"question_id", "hypothesis"}), so their official scorer grades it:
        cd <LongMemEval>/src/evaluation
        python3 evaluate_qa.py gpt-4o hypotheses.jsonl ../../data/longmemeval_oracle.json
        python3 print_qa_metrics.py gpt-4o hypotheses.jsonl.log ../../data/longmemeval_oracle.json
  * Runs EVERY system (fullcontext, mimir, oracle) through the SAME named LLM at
    temperature 0, so the comparison is apples-to-apples. The model id is recorded
    in the output; never run baselines through different models.
  * --dry-run builds every prompt and reports TOKENS PER SYSTEM with no LLM call.
    That token-efficiency number is itself offline, reproducible, and defensible
    (it is the honest version of the old doc's "fewer tokens" claim): how much
    context does each approach feed the model?

Systems:
  fullcontext  every haystack session concatenated (the "no memory" baseline)
  mimir        only the top-k sessions Mimir's hybrid retrieval returns
  oracle       only the gold evidence sessions (answer_session_ids) = upper bound

Usage:
  # Offline token-efficiency comparison (no key needed):
  python qa.py --data longmemeval_s_cleaned.json --systems fullcontext mimir --dry-run --max-instances 50

  # Real answers (needs a named OpenAI-compatible model):
  export OPENAI_API_KEY=...   # OPENAI_BASE_URL optional (OpenAI/OpenRouter/Anthropic-compat/local)
  python qa.py --data longmemeval_s_cleaned.json --systems fullcontext mimir --model gpt-4o-mini --k 5
  # -> writes hypotheses-<system>-<model>.jsonl ; grade each with LongMemEval's evaluate_qa.py
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from run import MimirServer, session_text, find_binary  # noqa: E402


def est_tokens(text: str) -> int:
    """Token estimate. Uses tiktoken if available, else a ~4-chars/token heuristic."""
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def build_context(system, inst, srv, qid, k):
    """Return (context_text, n_sessions_used) for the given system."""
    sessions = inst["haystack_sessions"]
    sids = inst["haystack_session_ids"]
    by_id = {sid: turns for sid, turns in zip(sids, sessions)}

    if system == "fullcontext":
        chosen = sids
    elif system == "oracle":
        chosen = inst.get("answer_session_ids", [])
    elif system == "mimir":
        # Ingest this instance, embed, hybrid-retrieve top-k sessions.
        for sid in sids:
            srv.call("mimir_remember", {"category": qid, "key": sid,
                                        "body_json": json.dumps({"note": session_text(by_id[sid])}),
                                        "type": "fact"})
        srv.call("mimir_embed", {"batch_category": qid, "batch_limit": 1000})
        r = srv.call("mimir_recall", {"query": inst["question"], "mode": "hybrid",
                                      "category": qid, "limit": k, "trust_weight": 0, "min_decay": 0})
        items = r.get("items", []) if isinstance(r, dict) else []
        chosen = [it.get("key") for it in items][:k]
    else:
        raise ValueError(system)

    blocks = []
    for sid in chosen:
        if sid in by_id:
            blocks.append(f"[session {sid}]\n{session_text(by_id[sid])}")
    return "\n\n".join(blocks), len(chosen)


PROMPT = ("You are answering a question using the provided chat history between a user and an "
          "assistant. Use only the history. If the answer is not present, say you don't know.\n\n"
          "Chat history:\n{context}\n\nQuestion: {question}\nAnswer:")


def call_llm(base_url, api_key, model, prompt):
    body = json.dumps({"model": model, "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {api_key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        out = json.loads(resp.read())
    return out["choices"][0]["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser(description="LongMemEval QA-accuracy answer generation")
    ap.add_argument("--data", required=True)
    ap.add_argument("--systems", nargs="+", default=["fullcontext", "mimir"],
                    choices=["fullcontext", "mimir", "oracle"])
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", ""),
                    help="Named LLM id (required unless --dry-run)")
    ap.add_argument("--k", type=int, default=5, help="Sessions retrieved for the mimir system")
    ap.add_argument("--bin", default=None)
    ap.add_argument("--max-instances", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="Build prompts + count tokens, no LLM call")
    ap.add_argument("--outdir", default=str(HERE))
    args = ap.parse_args()

    if not args.dry_run and not args.model:
        sys.exit("error: --model (or LLM_MODEL) is required unless --dry-run. "
                 "Run every system through the SAME named model.")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not args.dry_run and not api_key:
        sys.exit("error: OPENAI_API_KEY not set.")

    data = json.loads(Path(args.data).read_text(encoding="utf-8"))
    if args.max_instances:
        data = data[: args.max_instances]

    need_mimir = "mimir" in args.systems
    binary = find_binary(args.bin) if need_mimir else None
    db = str(Path(os.environ.get("TEMP") or "/tmp") / "mimir-qa.db")

    def wipe():
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db + ext)
            except OSError:
                pass

    tok = {s: 0 for s in args.systems}
    nsess = {s: 0 for s in args.systems}
    hyps = {s: [] for s in args.systems}
    t0 = time.time()

    for idx, inst in enumerate(data):
        qid = inst["question_id"]
        srv = None
        if need_mimir:
            wipe(); srv = MimirServer(binary, db)
        try:
            for system in args.systems:
                ctx, nused = build_context(system, inst, srv, qid, args.k)
                prompt = PROMPT.format(context=ctx, question=inst["question"])
                tok[system] += est_tokens(prompt)
                nsess[system] += nused
                if args.dry_run:
                    hyps[system].append({"question_id": qid, "hypothesis": ""})
                else:
                    try:
                        ans = call_llm(base_url, api_key, args.model, prompt)
                    except Exception as e:
                        ans = ""
                        print(f"  ! LLM error on {qid}/{system}: {e}", file=sys.stderr)
                    hyps[system].append({"question_id": qid, "hypothesis": ans})
        finally:
            if srv:
                srv.close()
        if (idx + 1) % 25 == 0:
            print(f"  {idx+1}/{len(data)}  ({time.time()-t0:.0f}s)", flush=True)
    if need_mimir:
        wipe()

    n = len(data)
    # Write hypotheses files (skip in dry-run; they'd be empty).
    if not args.dry_run:
        for system in args.systems:
            safe_model = args.model.replace("/", "_")
            out = Path(args.outdir) / f"hypotheses-{system}-{safe_model}.jsonl"
            out.write_text("\n".join(json.dumps(h) for h in hyps[system]) + "\n", encoding="utf-8")
            print(f"  wrote {out}  ({len(hyps[system])} answers)")

    # Token-efficiency report (real, offline, defensible).
    print(f"\nLongMemEval context cost - {n} instances"
          + ("  [DRY RUN: no LLM called]" if args.dry_run else f"  model={args.model}"))
    print(f"{'system':<13}{'avg sessions':>14}{'avg tokens/q':>14}{'total tokens':>15}")
    print("-" * 56)
    for system in args.systems:
        print(f"{system:<13}{nsess[system]/n:>14.1f}{tok[system]/n:>14.0f}{tok[system]:>15,}")
    if "fullcontext" in args.systems and "mimir" in args.systems and tok["mimir"]:
        ratio = tok["fullcontext"] / tok["mimir"]
        print(f"\nMimir feeds {ratio:.1f}x fewer tokens to the LLM than FullContext "
              f"(retrieval k={args.k}).")
    if not args.dry_run:
        print("\nNow grade each hypotheses file with LongMemEval's OFFICIAL judge:")
        print("  cd <LongMemEval>/src/evaluation")
        print(f"  python3 evaluate_qa.py <judge-model> <hypotheses-file> ../../data/longmemeval_oracle.json")
        print("  python3 print_qa_metrics.py <judge-model> <hypotheses-file>.log ../../data/longmemeval_oracle.json")
        print("Run every system's file through the SAME judge model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
