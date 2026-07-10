#!/usr/bin/env python3
"""Perseus Vault LongMemEval end-to-end QA harness (pinned answerer + pinned judge).

This is the SECOND LongMemEval stage and the head-to-head-vs-Zep harness (#475).
The first stage (session-level retrieval, offline, judge-free) lives in `run.py`.
This stage ingests each question's haystack into the REAL perseus-vault binary,
retrieves top-k sessions via hybrid recall, feeds them to a PINNED, NAMED
answerer LLM, and grades the answer with a PINNED, NAMED judge LLM against the
gold answer. Both run at temperature 0. The deprecated benchmarks/LONG_MEM_EVAL.md
explains why unpinned models/judges/splits made the OLD end-to-end numbers
untrustworthy; this harness exists so that never happens again.

Defaults (all overridable, always recorded in the report):
  answerer  gpt-4o-2024-08-06   (the GPT-4o snapshot closest to Zep's "GPT-4o" claim)
  judge     gpt-4o-2024-08-06
  split     s                   (longmemeval_s_cleaned.json, 500 instances)
  retrieval hybrid, top-k 10    (recall@10 = 99.2% per benchmark/longmemeval/report.json)

Systems (same idea as before; run every system through the SAME model):
  mimir        top-k sessions from perseus-vault hybrid retrieval  (the product)
  fullcontext  every haystack session concatenated                 (no-memory baseline)
  oracle       only the gold evidence sessions                     (upper bound)

API key: env OPENAI_API_KEY, else the file ~/.openai_key (contents, whitespace
stripped). The key is NEVER printed or logged. OPENAI_BASE_URL overrides the
endpoint (OpenAI-compatible servers work).

Cost control: a real run prints an upfront cost estimate + ETA and, above
--limit 50, refuses to proceed without --yes. Rate limiting: --tpm (default
25000, safely under OpenAI Tier-1 gpt-4o's 30k tokens/min) paces answerer AND
judge calls against a rolling 60s token budget, and 429s honor Retry-After.
Questions whose answerer still fails after all retries are recorded as
answer_error and EXCLUDED from the accuracy denominator — throttling can slow
a run but can never deflate the number. Opt-in; NOT part of any CI gate.

Usage:
  # Plumbing smoke test, no key and no network needed (stubbed answerer+judge):
  python qa.py --data longmemeval_s_cleaned.json --mock-llm --limit 5

  # Offline token-efficiency comparison (no key needed):
  python qa.py --data longmemeval_s_cleaned.json --systems fullcontext mimir --dry-run --limit 50

  # Cheap real smoke run (needs OPENAI_API_KEY or ~/.openai_key):
  python qa.py --data longmemeval_s_cleaned.json --limit 10

  # The full head-to-head number (500 questions; prints cost estimate first):
  python qa.py --data longmemeval_s_cleaned.json --yes

Dataset download (277 MB, public):
  curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
    -o longmemeval_s_cleaned.json

Output: qa_report.json (signed; per-category accuracy, per-question verdicts)
plus hypotheses-<system>-<model>.jsonl in LongMemEval's official format, so
LongMemEval's own evaluate_qa.py can cross-check our judge.
"""
import argparse
import hashlib
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
from run import MimirServer, session_text, find_binary  # noqa: E402

# Pinned defaults. Zep's published LongMemEval number is quoted as "GPT-4o";
# gpt-4o-2024-08-06 is the standard GPT-4o snapshot of that period and is the
# closest pinnable match. State the exact snapshot next to any number you quote.
DEFAULT_ANSWERER = "gpt-4o-2024-08-06"
DEFAULT_JUDGE = "gpt-4o-2024-08-06"

# USD per 1M tokens (input, output). Snapshot of OpenAI pricing, 2026-07.
# Used ONLY for the upfront cost estimate; update if prices move.
PRICING = {
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
}
FALLBACK_PRICE = (2.50, 10.00)  # assume gpt-4o pricing for unknown models

# Answer-generation prompt — ported VERBATIM from LongMemEval's official harness
# (xiaowu0162/LongMemEval, src/generation/run_generation.py, default non-CoT
# template). Carries NO "say you don't know if not present" instruction: an
# earlier revision of this harness added one, which made the model reflexively
# abstain on preference/aggregation questions and depressed the score ~18 points
# (single-session-preference collapsed to 1/30 — every failure the literal
# string "I don't know"). The official prompt relies on natural model behavior;
# abstention (_abs) instances are graded by the official abstention judge below.
# Matching the official prompt is what makes the number comparable to Zep's.
ANSWER_PROMPT = (
    "I will give you several history chats between you and a user. Please answer "
    "the question based on the relevant chat history.\n\n\n"
    "History Chats:\n\n{context}\n\n"
    "Current Date: {question_date}\n"
    "Question: {question}\n"
    "Answer:"
)


def get_anscheck_prompt(task, question, answer, response, abstention=False):
    """Judge prompt, ported VERBATIM from LongMemEval's official metric
    (xiaowu0162/LongMemEval, src/evaluation/evaluate_qa.py::get_anscheck_prompt).

    An earlier revision used one homegrown "does the response contain the gold
    answer" judge for every type. That deviated from the official per-type
    metric Zep was measured against: temporal answers were penalized for
    off-by-one day counts (official metric explicitly forgives them), and
    single-session-preference gold is a *rubric* describing a good personalized
    reply — the homegrown judge treated that paragraph as a string the answer
    had to contain, which almost nothing passes. The official per-type judge
    grades exactly as the benchmark defines; verified to reproduce our number
    bit-for-bit via the authors' own evaluate_qa.py.
    """
    if not abstention:
        if task in ('single-session-user', 'single-session-assistant', 'multi-session'):
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'temporal-reasoning':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'knowledge-update':
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        elif task == 'single-session-preference':
            template = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
        else:
            template = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    else:
        template = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
    return template.format(question, answer, response)


def est_tokens(text):
    """Token estimate. Uses tiktoken if available, else a ~4-chars/token heuristic."""
    try:
        import tiktoken
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    key_file = Path.home() / ".openai_key"
    if key_file.exists():
        key = key_file.read_text(encoding="utf-8").strip()
        if key:
            return key
    sys.exit(
        "error: no API key. Set OPENAI_API_KEY or put the key in ~/.openai_key.\n"
        "       (For a key-free plumbing check use --mock-llm; for token counts use --dry-run.)"
    )


class TokenBudget:
    """Rolling 60s token budget so a low-tier key never trips the TPM limit.

    acquire(est) blocks until `est` tokens fit in the current 60s window, then
    reserves them; settle(handle, actual) corrects the reservation to the real
    usage the API reported (or 0 for a rejected request). Thread-free by design
    (the harness is sequential)."""

    def __init__(self, tpm):
        self.tpm = tpm
        self.events = []  # [t_reserved, tokens]

    def _prune(self, now):
        self.events = [e for e in self.events if now - e[0] < 60.0]

    def acquire(self, est):
        if not self.tpm:
            return None
        need = min(est, self.tpm)  # an oversized single request waits for an empty window
        while True:
            now = time.time()
            self._prune(now)
            used = sum(e[1] for e in self.events)
            if used + need <= self.tpm:
                break
            wait = (self.events[0][0] + 60.0 - now) if self.events else 1.0
            time.sleep(max(0.25, min(wait, 60.0)))
        ev = [time.time(), est]
        self.events.append(ev)
        return ev

    def settle(self, ev, actual_tokens):
        if ev is not None and actual_tokens is not None:
            ev[1] = actual_tokens


def _retry_delay(err, attempt):
    """Delay before a retry: honor Retry-After / the 429 body's 'try again in Xs'
    when present, else exponential backoff."""
    if isinstance(err, urllib.error.HTTPError):
        ra = (err.headers.get("Retry-After") or "").strip() if err.headers else ""
        if ra:
            try:
                return min(120.0, float(ra)) + random.uniform(0, 1)
            except ValueError:
                pass  # HTTP-date form; fall through
        try:
            body = getattr(err, "_body_cache", None)
            if body is None:
                body = err.read().decode("utf-8", "replace")
                err._body_cache = body
            m = re.search(r"try again in ([0-9.]+)\s*(ms|s)", body, re.IGNORECASE)
            if m:
                secs = float(m.group(1)) / (1000.0 if m.group(2).lower() == "ms" else 1.0)
                return min(120.0, secs) + random.uniform(0.5, 1.5)
        except Exception:
            pass
    return min(60.0, 2 ** attempt) + random.uniform(0, 1)


def call_llm(base_url, api_key, model, prompt, budget=None, max_retries=12):
    """One chat completion at temperature 0. Token-paced via `budget`, honors
    Retry-After on 429, exponential backoff otherwise. Raises only after
    max_retries — callers record that as answer_error, never as a wrong answer."""
    body = json.dumps({
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    url = base_url.rstrip("/") + "/chat/completions"
    est = est_tokens(prompt) + 300  # request + response headroom
    for attempt in range(max_retries):
        ev = budget.acquire(est) if budget else None
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read())
            if budget:
                usage = out.get("usage") or {}
                budget.settle(ev, usage.get("total_tokens") or est)
            return out["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if budget:
                    budget.settle(ev, 0)  # rejected requests don't consume TPM
                if attempt == max_retries - 1:
                    raise
                delay = _retry_delay(e, attempt)
                print(f"  ! HTTP {e.code}, retrying in {delay:.0f}s "
                      f"({attempt + 1}/{max_retries})", file=sys.stderr)
                time.sleep(delay)
            else:
                # 4xx other than 429: not transient. Do not echo headers (key safety).
                raise RuntimeError(f"LLM call failed: HTTP {e.code} {e.reason}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if budget:
                budget.settle(ev, 0)
            if attempt == max_retries - 1:
                raise
            delay = min(60.0, 2 ** attempt) + random.uniform(0, 1)
            print(f"  ! transient error ({type(e).__name__}), retrying in {delay:.0f}s "
                  f"({attempt + 1}/{max_retries})", file=sys.stderr)
            time.sleep(delay)


# ── Mock LLM (plumbing smoke test; deterministic, no key, no network) ──────────
def mock_answer(inst, idx):
    """Even instances answer with the gold text, odd ones abstain — so BOTH
    judge verdict paths (yes and no) are exercised end-to-end."""
    return inst["answer"] if idx % 2 == 0 else "I don't know."


def mock_judge(inst, answer):
    ans = answer.lower()
    if inst["question_id"].endswith("_abs"):
        abstained = any(p in ans for p in ("don't know", "do not know", "not available",
                                           "no information", "cannot"))
        return "yes" if abstained else "no"
    return "yes" if str(inst["answer"]).lower() in ans else "no"


def session_note(date, turns):
    """What gets ingested per session: the flattened turns, date-stamped so the
    answerer (and the bi-temporal engine) can reason about WHEN it happened."""
    prefix = f"session date: {date}\n" if date else ""
    return prefix + session_text(turns)


def build_context(system, inst, srv, qid, k):
    """Return (context_text, [chosen_session_ids]) for the given system."""
    sessions = inst["haystack_sessions"]
    sids = inst["haystack_session_ids"]
    dates = inst.get("haystack_dates") or [None] * len(sids)
    by_id = {sid: (turns, d) for sid, turns, d in zip(sids, sessions, dates)}

    if system == "fullcontext":
        chosen = sids
    elif system == "oracle":
        chosen = inst.get("answer_session_ids", [])
    elif system == "mimir":
        # Ingest this instance's haystack, embed, hybrid-retrieve top-k sessions.
        for sid in sids:
            turns, d = by_id[sid]
            srv.call("mimir_remember", {"category": qid, "key": sid,
                                        "body_json": json.dumps({"note": session_note(d, turns)}),
                                        "type": "fact"})
        srv.call("mimir_embed", {"batch_category": qid, "batch_limit": 1000})
        r = srv.call("mimir_recall", {"query": inst["question"], "mode": "hybrid",
                                      "category": qid, "limit": k, "trust_weight": 0,
                                      "min_decay": 0})
        items = r.get("items", []) if isinstance(r, dict) else []
        chosen = [it.get("key") for it in items][:k]
    else:
        raise ValueError(system)

    blocks = []
    for sid in chosen:
        if sid in by_id:
            turns, d = by_id[sid]
            hdr = f"[session {sid}" + (f" | {d}" if d else "") + "]"
            blocks.append(f"{hdr}\n{session_text(turns)}")
    return "\n\n".join(blocks), [s for s in chosen if s in by_id]


def price_for(model):
    return PRICING.get(model, FALLBACK_PRICE)


def estimate_cost(data, systems, k, model, judge):
    """Rough upfront USD estimate from dataset shape (4-chars/token heuristic)."""
    sample = data[:min(len(data), 20)]
    sess_toks, sess_counts = [], []
    for inst in sample:
        sess_counts.append(len(inst["haystack_sessions"]))
        for turns in inst["haystack_sessions"][:10]:
            sess_toks.append(est_tokens(session_text(turns)))
    avg_sess = sum(sess_toks) / max(1, len(sess_toks))
    avg_n = sum(sess_counts) / max(1, len(sess_counts))

    ctx_per_system = {"mimir": k * avg_sess, "fullcontext": avg_n * avg_sess,
                      "oracle": 2 * avg_sess}
    n = len(data)
    answer_out, judge_in_fixed, judge_out = 150, 250, 5
    a_in, a_out = price_for(model)
    j_in, j_out = price_for(judge)
    total, total_toks, lines = 0.0, 0, []
    for system in systems:
        ans_in_toks = n * (ctx_per_system[system] + 120)
        sys_toks = ans_in_toks + n * (answer_out + judge_in_fixed + answer_out + judge_out)
        cost = (ans_in_toks / 1e6 * a_in) + (n * answer_out / 1e6 * a_out) \
             + (n * (judge_in_fixed + answer_out) / 1e6 * j_in) + (n * judge_out / 1e6 * j_out)
        total += cost
        total_toks += sys_toks
        lines.append(f"  {system:<13}~{ans_in_toks / 1e6:5.1f}M answerer input tokens"
                     f"  -> est ${cost:,.2f}")
    unknown = [m for m in {model, judge} if m not in PRICING]
    note = f"  (unknown pricing for {', '.join(unknown)}; assumed gpt-4o rates)" if unknown else ""
    return total, total_toks, "\n".join(lines) + (f"\n{note}" if note else "")


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def binary_version(binary):
    try:
        return subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=30).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main():
    ap = argparse.ArgumentParser(description="LongMemEval end-to-end QA accuracy (pinned answerer + judge)")
    ap.add_argument("--data", default=None,
                    help="Path to longmemeval_<split>_cleaned.json (default: ./longmemeval_<split>_cleaned.json)")
    ap.add_argument("--split", default="s", choices=["s", "m"],
                    help="LongMemEval split; 's' (500 instances) is what Zep reports on")
    ap.add_argument("--systems", nargs="+", default=["mimir"],
                    choices=["fullcontext", "mimir", "oracle"],
                    help="Run every system through the SAME model (default: mimir only)")
    ap.add_argument("--model", default=DEFAULT_ANSWERER, help=f"Answerer model id (default {DEFAULT_ANSWERER})")
    ap.add_argument("--judge", default=DEFAULT_JUDGE, help=f"Judge model id (default {DEFAULT_JUDGE})")
    ap.add_argument("--k", type=int, default=10, help="Sessions retrieved for the mimir system (default 10)")
    ap.add_argument("--limit", type=int, default=0, help="Only run the first N instances (0 = all; smoke tests)")
    ap.add_argument("--bin", default=None, help="perseus-vault binary (else auto-located / MIMIR_BIN)")
    ap.add_argument("--mock-llm", action="store_true",
                    help="Stub the answerer+judge (deterministic, no key, no network): proves the plumbing")
    ap.add_argument("--dry-run", action="store_true",
                    help="Build prompts + count tokens only; no LLM, no judge, no report")
    ap.add_argument("--yes", action="store_true",
                    help="Accept the printed cost estimate (required for real runs above 50 instances)")
    ap.add_argument("--tpm", type=int, default=25000,
                    help="Token-per-minute budget for API pacing (default 25000, safely under "
                         "OpenAI Tier-1 gpt-4o's 30k TPM; 0 disables pacing). Answerer and "
                         "judge calls share the budget.")
    ap.add_argument("--resume", action="store_true",
                    help="#518: resume from the progress journal — already-judged questions are "
                         "skipped (their verdicts reload from disk); errored questions are "
                         "retried. The journal must match this run's config exactly.")
    ap.add_argument("--journal", default=None,
                    help="Progress journal path (default: <outdir>/qa_progress-<split>-<model>.jsonl). "
                         "Appended after EVERY judged question, so a killed run loses at most "
                         "the question in flight.")
    ap.add_argument("--out", default=str(HERE / "qa_report.json"))
    ap.add_argument("--outdir", default=str(HERE), help="Where hypotheses-*.jsonl files go")
    args = ap.parse_args()

    data_path = Path(args.data) if args.data else HERE / f"longmemeval_{args.split}_cleaned.json"
    if not data_path.exists():
        sys.exit(f"error: dataset not found at {data_path}\n"
                 "Download (public, 277 MB for _s):\n"
                 f"  curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/"
                 f"resolve/main/longmemeval_{args.split}_cleaned.json -o {data_path}")
    full = json.loads(data_path.read_text(encoding="utf-8"))
    split_size = len(full)
    data = full[: args.limit] if args.limit else full

    live = not (args.mock_llm or args.dry_run)
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = get_api_key() if live else ""

    if not args.dry_run:
        cost, toks, detail = estimate_cost(data, args.systems, args.k, args.model, args.judge)
        print(f"Estimated cost for {len(data)} instances x {len(args.systems)} system(s) "
              f"(answerer={args.model}, judge={args.judge}):\n{detail}\n"
              f"  total     est ${cost:,.2f}"
              + ("   [mock run: $0 actually spent]" if args.mock_llm else ""))
        if live and args.tpm:
            eta_min = toks / args.tpm
            eta = f"{eta_min / 60:.1f}h" if eta_min >= 90 else f"{eta_min:.0f} min"
            print(f"  pacing    ~{toks / 1e6:.1f}M est tokens at --tpm {args.tpm:,}"
                  f"  -> ETA ~{eta} (rate-limit bound, not compute bound)")
        if live and len(data) > 50 and not args.yes:
            sys.exit("\nThis is a paid full run. Re-run with --yes to accept the estimate "
                     "(or use --limit 10 for a cheap smoke run, --mock-llm for free plumbing).")

    need_mimir = "mimir" in args.systems
    binary = find_binary(args.bin) if need_mimir else None
    bin_ver = binary_version(binary) if binary else "n/a"
    db = str(Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp") / "mimir-qa.db")

    def wipe():
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(db + ext)
            except OSError:
                pass

    tok = {s: 0 for s in args.systems}
    nsess = {s: 0 for s in args.systems}
    hyps = {s: [] for s in args.systems}
    verdicts = []  # {question_id, question_type, system, correct, error, judge_raw}
    budget = TokenBudget(args.tpm) if (live and args.tpm) else None
    t0 = time.time()

    # ── #518: crash-safe progress journal + resume ─────────────────────────
    # One JSON line per judged (question, system), appended and flushed as it
    # happens — a killed run (crash, reboot, quota exhaustion, parent-process
    # teardown) loses at most the question in flight, never the run. The first
    # line pins the run config; --resume refuses a mismatched journal rather
    # than silently blending two configurations. The signed report is still
    # produced ONLY at completion over the full verdict set: a partial journal
    # is never signed and never quotable.
    model_tag = ("mock" if args.mock_llm else args.model).replace("/", "_")
    journal_path = Path(args.journal) if args.journal else \
        Path(args.outdir) / f"qa_progress-{args.split}-{model_tag}.jsonl"
    run_config = {"split": args.split, "n": len(data),
                  "systems": sorted(args.systems),
                  "model": "mock" if args.mock_llm else args.model,
                  "judge": "mock" if args.mock_llm else args.judge,
                  "k": args.k}
    done = {}
    journal = None
    if not args.dry_run:
        resume_ok = False
        if args.resume and journal_path.exists():
            lines = [json.loads(ln) for ln in
                     journal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if not lines or "_config" not in lines[0]:
                sys.exit(f"error: --resume: {journal_path} has no config header — "
                         "not a progress journal (delete it or pass --journal).")
            if lines[0]["_config"] != run_config:
                sys.exit("error: --resume config mismatch:\n"
                         f"  journal: {lines[0]['_config']}\n  current: {run_config}\n"
                         "Delete the journal (or pass --journal) to start fresh.")
            for rec in lines[1:]:
                # Completed verdicts reload; errored questions retry.
                if rec.get("error") is None:
                    done[(rec["question_id"], rec["system"])] = rec
            resume_ok = True
            print(f"  resume: {len(done)} judged answers reloaded from "
                  f"{journal_path.name}; errored/unfinished questions will run.")
        journal = open(journal_path, "a" if resume_ok else "w", encoding="utf-8")
        if not resume_ok:
            journal.write(json.dumps({"_config": run_config}) + "\n")
            journal.flush()
        # Seed the accumulators from the reloaded verdicts so the final report
        # covers the WHOLE run, not just this process's share.
        for rec in done.values():
            tok[rec["system"]] += rec.get("tokens_est", 0)
            nsess[rec["system"]] += rec.get("sessions", 0)
            hyps[rec["system"]].append({"question_id": rec["question_id"],
                                        "hypothesis": rec.get("hypothesis", "")})
            verdicts.append({k: rec.get(k) for k in
                             ("question_id", "question_type", "system",
                              "abstention", "correct", "error", "judge_raw")})

    def record(rec, hypothesis, tokens_est, sessions):
        """Append a verdict to memory AND the crash-safe journal."""
        verdicts.append(rec)
        if journal:
            journal.write(json.dumps({**rec, "hypothesis": hypothesis,
                                      "tokens_est": tokens_est,
                                      "sessions": sessions}) + "\n")
            journal.flush()

    for idx, inst in enumerate(data):
        qid = inst["question_id"]
        qtype = inst.get("question_type", "unknown")
        is_abs = qid.endswith("_abs")
        # #518: fully-judged instances skip even the (expensive) re-ingest.
        if not args.dry_run and all((qid, s) in done for s in args.systems):
            continue
        srv = None
        if need_mimir:
            wipe()
            srv = MimirServer(binary, db)
        try:
            for system in args.systems:
                if (qid, system) in done:
                    continue
                ctx, chosen = build_context(system, inst, srv, qid, args.k)
                prompt = ANSWER_PROMPT.format(context=ctx, question=inst["question"],
                                              question_date=inst.get("question_date", "unknown"))
                tok[system] += est_tokens(prompt)
                nsess[system] += len(chosen)
                if args.dry_run:
                    hyps[system].append({"question_id": qid, "hypothesis": ""})
                    continue

                q_tokens = est_tokens(prompt)
                if args.mock_llm:
                    ans = mock_answer(inst, idx)
                else:
                    try:
                        ans = call_llm(base_url, api_key, args.model, prompt, budget)
                    except Exception as e:
                        # A rate-limited/failed question must NEVER deflate accuracy:
                        # record it as answer_error and exclude it from the denominator.
                        # (--resume retries it: errored records don't enter `done`.)
                        print(f"  !! ANSWER_ERROR on {qid}/{system} (excluded from accuracy): {e}",
                              file=sys.stderr)
                        hyps[system].append({"question_id": qid, "hypothesis": ""})
                        record({"question_id": qid, "question_type": qtype,
                                "system": system, "abstention": is_abs,
                                "correct": None, "error": "answer_error",
                                "judge_raw": None}, "", q_tokens, len(chosen))
                        continue
                hyps[system].append({"question_id": qid, "hypothesis": ans})

                jp = get_anscheck_prompt(qtype, inst["question"], inst["answer"],
                                         ans or "(no answer)", abstention=is_abs)
                if args.mock_llm:
                    jraw = mock_judge(inst, ans)
                else:
                    try:
                        jraw = call_llm(base_url, api_key, args.judge, jp, budget)
                    except Exception as e:
                        print(f"  !! JUDGE_ERROR on {qid}/{system} (excluded from accuracy): {e}",
                              file=sys.stderr)
                        record({"question_id": qid, "question_type": qtype,
                                "system": system, "abstention": is_abs,
                                "correct": None, "error": "judge_error",
                                "judge_raw": None}, ans, q_tokens, len(chosen))
                        continue
                correct = jraw.strip().lower().startswith("yes")
                record({"question_id": qid, "question_type": qtype, "system": system,
                        "abstention": is_abs, "correct": correct, "error": None,
                        "judge_raw": jraw.strip()[:40]}, ans, q_tokens, len(chosen))
        finally:
            if srv:
                srv.close()
        # #518: per-question progress with a running graded accuracy, so a
        # backgrounded run is observable from its output file.
        graded_so_far = [v for v in verdicts if v.get("error") is None]
        acc_so_far = (sum(1 for v in graded_so_far if v["correct"])
                      / max(1, len(graded_so_far)) * 100)
        print(f"  {idx + 1}/{len(data)}  graded={len(graded_so_far)} "
              f"acc={acc_so_far:.1f}%  ({time.time() - t0:.0f}s)", flush=True)
    if need_mimir:
        wipe()
    if journal:
        journal.close()

    n = len(data)
    # Hypotheses files in LongMemEval's official format, so their evaluate_qa.py
    # can independently cross-check our judge. Skipped in dry-run (empty).
    # (On a resumed run, reloaded answers come first — evaluate_qa.py keys on
    # question_id, so order is immaterial.)
    if not args.dry_run:
        for system in args.systems:
            out = Path(args.outdir) / f"hypotheses-{system}-{model_tag}.jsonl"
            out.write_text("\n".join(json.dumps(h) for h in hyps[system]) + "\n", encoding="utf-8")
            print(f"  wrote {out}  ({len(hyps[system])} answers)")

    # Token-efficiency table (offline, defensible; the honest "fewer tokens" claim).
    print(f"\nLongMemEval context cost - {n} instances"
          + ("  [DRY RUN: no LLM called]" if args.dry_run
             else ("  [MOCK LLM]" if args.mock_llm else f"  model={args.model}")))
    print(f"{'system':<13}{'avg sessions':>14}{'avg tokens/q':>14}{'total tokens':>15}")
    print("-" * 56)
    for system in args.systems:
        print(f"{system:<13}{nsess[system] / n:>14.1f}{tok[system] / n:>14.0f}{tok[system]:>15,}")
    if "fullcontext" in args.systems and "mimir" in args.systems and tok["mimir"]:
        print(f"\nmimir feeds {tok['fullcontext'] / tok['mimir']:.1f}x fewer tokens to the LLM "
              f"than fullcontext (k={args.k}).")
    if args.dry_run:
        return 0

    # ── Accuracy report ────────────────────────────────────────────────────────
    systems_report = {}
    for system in args.systems:
        vs = [v for v in verdicts if v["system"] == system]
        # Errored questions (rate limit exhausted, judge failure) are EXCLUDED
        # from the accuracy denominator — a throttled run must not deflate the
        # published number. They are counted prominently instead.
        graded = [v for v in vs if v["error"] is None]
        answer_errors = sum(1 for v in vs if v["error"] == "answer_error")
        judge_errors = sum(1 for v in vs if v["error"] == "judge_error")
        by_type = {}
        for v in graded:
            bt = by_type.setdefault(v["question_type"], {"n": 0, "correct": 0})
            bt["n"] += 1
            bt["correct"] += int(v["correct"])
        for bt in by_type.values():
            bt["accuracy"] = round(bt["correct"] / bt["n"], 4)
        abst = [v for v in graded if v["abstention"]]
        systems_report[system] = {
            "n_attempted": len(vs),
            "n_graded": len(graded),
            "answer_errors": answer_errors,
            "judge_errors": judge_errors,
            "accuracy": round(sum(v["correct"] for v in graded) / max(1, len(graded)), 4),
            "by_question_type": by_type,
            "abstention": {"n": len(abst),
                           "accuracy": round(sum(v["correct"] for v in abst) / len(abst), 4) if abst else None},
            "avg_context_tokens_est": round(tok[system] / n),
            "avg_sessions_in_context": round(nsess[system] / n, 1),
        }
        if answer_errors or judge_errors:
            print(f"  !! {system}: {answer_errors} answer_error(s) + {judge_errors} judge_error(s) "
                  f"EXCLUDED from the accuracy denominator ({len(graded)}/{len(vs)} graded). "
                  "Re-run those questions (lower --tpm or higher tier) before publishing.",
                  file=sys.stderr)

    # Signature over the verdict set (same convention as run.py's signed report).
    sig_payload = json.dumps({
        "benchmark": "perseus-vault-longmemeval-qa",
        "split": f"longmemeval_{args.split}", "n": n,
        "answerer": "mock" if args.mock_llm else args.model,
        "judge": "mock" if args.mock_llm else args.judge,
        "verdicts": sorted([v["question_id"], v["system"], v["correct"]] for v in verdicts),
    }, sort_keys=True)
    signature = hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()

    report = {
        "benchmark": "perseus-vault-longmemeval-qa",
        "metric": "end-to-end QA accuracy (pinned answerer + pinned judge vs gold answers)",
        "dataset": data_path.name,
        "split": f"longmemeval_{args.split}",
        "split_size": split_size,
        "n_instances": n,
        "mock_llm": args.mock_llm,
        "answerer_model": "mock" if args.mock_llm else args.model,
        "judge_model": "mock" if args.mock_llm else args.judge,
        "temperature": 0,
        "retrieval": {"mode": "hybrid", "k": args.k, "embedding": "bundled-onnx"},
        "systems": systems_report,
        "commit": git_commit(),
        "binary": Path(binary).name if binary else None,
        "binary_version": bin_ver,
        "platform": platform.platform(),
        "hardware": {"machine": platform.machine(), "processor": platform.processor(),
                     "cpu_count": os.cpu_count()},
        "elapsed_secs": round(time.time() - t0, 1),
        "tpm_budget": args.tpm if live else None,
        "signature_sha256": signature,
        "per_question": [{"question_id": v["question_id"], "question_type": v["question_type"],
                          "system": v["system"], "correct": v["correct"],
                          "error": v["error"]} for v in verdicts],
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"\nLongMemEval end-to-end QA - split=longmemeval_{args.split} n={n}"
          + ("  [MOCK LLM: plumbing only, NOT a real accuracy number]" if args.mock_llm
             else f"  answerer={args.model} judge={args.judge}"))
    for system in args.systems:
        sr = systems_report[system]
        err_note = (f", {sr['answer_errors'] + sr['judge_errors']} errored+excluded"
                    if (sr["answer_errors"] or sr["judge_errors"]) else "")
        print(f"\n  {system}: accuracy {sr['accuracy'] * 100:.1f}%  "
              f"({sr['n_graded']} graded of {sr['n_attempted']} attempted{err_note})")
        for qt, bt in sorted(sr["by_question_type"].items()):
            print(f"    {qt:<28}{bt['correct']:>4}/{bt['n']:<4}  {bt['accuracy'] * 100:5.1f}%")
        if sr["abstention"]["n"]:
            print(f"    {'(abstention subset)':<28}{'':>9}  {sr['abstention']['accuracy'] * 100:5.1f}%")
    print(f"\nsignature: {signature[:16]}...  ->  {args.out}")
    if args.mock_llm:
        print("Reminder: --mock-llm accuracy is meaningless by construction (~50%); "
              "it only proves ingest -> retrieval -> context -> report plumbing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
