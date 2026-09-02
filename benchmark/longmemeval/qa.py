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
  stateless    no history at all: question only                    (arm 0 — why memory exists)
  perseus_vault        top-k sessions from perseus-vault hybrid retrieval  (the product)
  fullcontext  every haystack session concatenated                 (no-memory-layer baseline)
  oracle       only the gold evidence sessions                     (upper bound)

Every live verdict records the API-billed prompt/completion tokens for both the
answerer and the judge (`ans_usage`/`judge_usage`, aggregated per system as
`api_usage_tokens`). Provider-billed tokens — not estimates — are what feed the
CPST (cost per successfully completed task) accounting in cpst.py.

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
  python qa.py --data longmemeval_s_cleaned.json --systems fullcontext perseus_vault --dry-run --limit 50

  # Cheap real smoke run (needs OPENAI_API_KEY or ~/.openai_key):
  python qa.py --data longmemeval_s_cleaned.json --limit 10

  # The full head-to-head number (500 questions; prints cost estimate first):
  python qa.py --data longmemeval_s_cleaned.json --yes

Dataset download (277 MB, public):
  curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
    -o longmemeval_s_cleaned.json

Output: qa_report.json (content-hashed; per-category accuracy, per-question verdicts)
plus hypotheses-<system>-<model>-<prompt-lane>.jsonl in LongMemEval's official
format, so LongMemEval's own evaluate_qa.py can cross-check our judge without
allowing plain and official-CoT artifacts to overwrite one another.
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
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
from context_assembly import (  # noqa: E402
    assemble_assistant_recall_ledger,
    assemble_evidence_ledger,
    assemble_ranked_snippets,
    stable_ranked_items,
)
from run import (  # noqa: E402
    AGENT,
    WORKSPACE,
    PerseusVaultServer,
    admitted_remember,
    find_binary,
    session_text,
)
from benchmark.package.common.replay import (
    RECALL_WIRE_SCHEMA_VERSION,
    normalize_recall_response,
    prepare_recall_preflight,
    sha256_text as replay_sha256_text,
    stable_json as replay_stable_json,
    validate_recall_preflight,
)  # noqa: E402

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

# LongMemEval's official chain-of-thought answer prompt, copied from
# xiaowu0162/LongMemEval/src/generation/run_generation.py. The complete model
# response is retained for the official per-type judge and hypotheses artifact.
ANSWER_PROMPT_COT = (
    "I will give you several history chats between you and a user. Please answer "
    "the question based on the relevant chat history. Answer the question step by step: "
    "first extract all the relevant information, and then reason over the information to "
    "get the answer.\n\n\n"
    "History Chats:\n\n{}\n\n"
    "Current Date: {}\n"
    "Question: {}\n"
    "Answer (step by step):"
)

HYPOTHESIS_MODE = "complete-response"


def hypothesis_for_judge(text, cot=False):
    """Return the complete response expected by LongMemEval's official judge.

    CoT responses must not be reduced to the tail after ``Answer:``: the
    official evaluator grades the complete ``hypothesis`` string. The ``cot``
    argument is retained as an explicit call-site contract and for future
    protocol assertions.
    """
    del cot
    return text.strip() if text else text


def hypothesis_artifact_name(system, model, cot=False):
    """Return a prompt-lane-specific official hypothesis artifact name."""
    model_tag = str(model).replace("/", "_")
    prompt_tag = "official-cot" if cot else "plain"
    return f"hypotheses-{system}-{model_tag}-{prompt_tag}.jsonl"


def extract_cot_answer(text):
    """Extract a final-answer tail for optional diagnostics only.

    This helper is deliberately not used for official judging or hypothesis
    artifacts, because LongMemEval's evaluator receives the full response.
    """
    if not text:
        return text
    lower = text.lower()
    idx = lower.rfind("answer:")
    marker = text[idx + len("answer:"):].strip() if idx != -1 else ""
    return marker if marker else text.strip()


def write_checkpoint(journal, record):
    """Append one JSONL checkpoint and force it to durable storage."""
    journal.write(json.dumps(record) + "\n")
    journal.flush()
    os.fsync(journal.fileno())


_QA_JOURNAL_FIELDS = frozenset({
    "question_id", "question_type", "system", "abstention", "correct", "error",
    "judge_raw", "ans_usage", "judge_usage", "hypothesis", "tokens_est", "sessions",
    "record_sha256",
})
_QA_USAGE_FIELDS = frozenset({"prompt_tokens", "completion_tokens"})


def _qa_journal_digest(record):
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    return replay_sha256_text(replay_stable_json(payload))


def _seal_qa_journal_record(record):
    sealed = dict(record)
    sealed["record_sha256"] = _qa_journal_digest(sealed)
    return sealed


def _validate_qa_usage(value, field):
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != _QA_USAGE_FIELDS:
        raise ValueError(f"{field} is malformed")
    for name, tokens in value.items():
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError(f"{field}.{name} is malformed")


def validate_qa_resume_record(
    record,
    *,
    instance,
    systems,
    require_preflight=True,
    preflight_runtime=None,
):
    """Validate one QA journal row against the current dataset instance."""
    if not isinstance(record, dict):
        raise ValueError("QA journal record must be an object")
    allowed = set(_QA_JOURNAL_FIELDS)
    if require_preflight:
        allowed.add("preflight")
    if set(record) != allowed:
        raise ValueError("QA journal record fields are incomplete or unknown")
    if not isinstance(record.get("record_sha256"), str) or record["record_sha256"] != _qa_journal_digest(record):
        raise ValueError("QA journal record digest mismatch")
    question_id = instance.get("question_id")
    if record.get("question_id") != question_id:
        raise ValueError("QA journal question_id differs from current dataset")
    expected_type = instance.get("question_type", "unknown")
    if record.get("question_type") != expected_type:
        raise ValueError("QA journal question_type differs from current dataset")
    if record.get("system") not in set(systems):
        raise ValueError("QA journal system is not in the current run")
    expected_abstention = isinstance(question_id, str) and question_id.endswith("_abs")
    if record.get("abstention") is not expected_abstention:
        raise ValueError("QA journal abstention flag differs from current dataset")
    error = record.get("error")
    if error not in {None, "answer_error", "judge_error"}:
        raise ValueError("QA journal error status is invalid")
    correct = record.get("correct")
    if error is None:
        if not isinstance(correct, bool) or not isinstance(record.get("judge_raw"), str):
            raise ValueError("graded QA journal record is malformed")
    elif correct is not None or record.get("judge_raw") is not None:
        raise ValueError("errored QA journal record contains a verdict")
    if not isinstance(record.get("hypothesis"), str):
        raise ValueError("QA journal hypothesis is malformed")
    for field in ("tokens_est", "sessions"):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"QA journal {field} is malformed")
    _validate_qa_usage(record.get("ans_usage"), "QA journal ans_usage")
    _validate_qa_usage(record.get("judge_usage"), "QA journal judge_usage")
    if require_preflight:
        if not isinstance(record.get("preflight"), dict):
            raise ValueError("QA journal preflight is malformed")
        try:
            if preflight_runtime is None:
                validate_recall_preflight(record["preflight"])
            else:
                validate_recall_preflight(record["preflight"], **preflight_runtime)
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError("QA journal preflight commitment failed validation") from exc


def _validate_qa_resume_record(
    record,
    *,
    instance,
    systems,
    require_preflight=True,
    preflight_runtime=None,
):
    validate_qa_resume_record(
        record,
        instance=instance,
        systems=systems,
        require_preflight=require_preflight,
        preflight_runtime=preflight_runtime,
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


def call_llm(base_url, api_key, model, prompt, budget=None, max_retries=12, max_tokens=None):
    """One chat completion at temperature 0. Token-paced via `budget`, honors
    Retry-After on 429, exponential backoff otherwise. Raises only after
    max_retries — callers record that as answer_error, never as a wrong answer.
    `max_tokens` caps the completion (the #579 CoT prompt needs room to reason:
    pass ~1200; None leaves the provider default)."""
    payload = {
        "model": model, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    body = json.dumps(payload).encode()
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
            usage = out.get("usage") or {}
            if budget:
                budget.settle(ev, usage.get("total_tokens") or est)
            # API-reported usage is the ground truth for cost accounting (#CPST):
            # estimates pace the budget, but only provider-billed tokens are quotable.
            return (out["choices"][0]["message"]["content"].strip(),
                    {"prompt_tokens": usage.get("prompt_tokens", 0),
                     "completion_tokens": usage.get("completion_tokens", 0)})
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
    judge verdict paths (yes and no) are exercised end-to-end. Gold answers
    can be non-string (ints in longmemeval_s), so coerce."""
    return str(inst["answer"]) if idx % 2 == 0 else "I don't know."


def mock_judge(inst, answer):
    ans = str(answer).lower()
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


SHARED_FACT_KEY = "__ku_fact__"


def _date_ms(datestr):
    """LongMemEval session date ('2023/08/11 (Fri) 00:01') -> unix ms.
    Keeps the time-of-day: same-day updates must still order correctly."""
    s = re.sub(r"\s*\([^)]*\)\s*", " ", datestr).strip()
    try:
        d = datetime.strptime(s, "%Y/%m/%d %H:%M")
    except ValueError:
        d = datetime.strptime(s.split(" ")[0], "%Y/%m/%d")
    return int(d.timestamp() * 1000)


def build_context(
    system, inst, srv, qid, k, ku_shared=False, context_assembly="full",
    assembly_k=20, context_budget=32768, assembly_windows=2,
    context_guidance="none", ledger_budget=12000
):
    """Return (context_text, [chosen_session_ids]) for the given system.

    With ku_shared, the perseus_vault arm ingests the gold (fact-version) sessions
    under ONE shared key with valid_from = session date — the PRODUCT shape
    (INGEST_590.md demo B): `perseus_vault_remember` collapses versions to a live
    latest-wins row and stale versions go to `entity_history`. Grouping uses
    the dataset's evidence labels — authoring-time knowledge, exactly what a
    real caller has when it re-remembers a fact under its key."""
    if context_assembly not in {"full", "ranked-snippets", "evidence-ledger", "assistant-recall"}:
        raise ValueError(f"unknown context assembly: {context_assembly}")
    if context_guidance not in {"none", "preference", "preference-structured", "evidence-structured"}:
        raise ValueError(f"unknown context guidance: {context_guidance}")
    if context_guidance != "none" and context_assembly != "ranked-snippets":
        raise ValueError("context guidance requires ranked-snippets assembly")
    if assembly_k <= 0 or context_budget <= 0 or assembly_windows <= 0:
        raise ValueError("assembly_k, context_budget, and assembly_windows must be positive")
    if ledger_budget <= 0 or ledger_budget > 16000:
        raise ValueError("ledger_budget must be between 1 and 16000 tokens")
    sessions = inst["haystack_sessions"]
    sids = inst["haystack_session_ids"]
    dates = inst.get("haystack_dates") or [None] * len(sids)
    by_id = {sid: (turns, d) for sid, turns, d in zip(sids, sessions, dates)}

    if system == "stateless":
        # No memory of any kind: the agent answers from the question alone.
        # This is the "why does the memory category exist at all" arm.
        chosen = []
    elif system == "fullcontext":
        chosen = sids
    elif system == "oracle":
        chosen = inst.get("answer_session_ids", [])
    elif system == "perseus-vault":
        # Ingest this instance's haystack, embed, hybrid-retrieve top-k sessions.
        # Gold labels are needed only for the explicit shared-key product arm;
        # ordinary retrieval and every answer-facing projection stay gold-blind.
        gold = (inst.get("answer_session_ids", []) or []) if ku_shared else []
        dated_gold = sorted([g for g in gold if by_id.get(g, (None, None))[1]],
                            key=lambda g: _date_ms(by_id[g][1]))
        shared = set(dated_gold) if (ku_shared and len(dated_gold) >= 2) else set()
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
                valid_from_unix_ms=_date_ms(d),
            )
        srv.call("perseus_vault_embed", {"batch_category": qid, "batch_limit": 1000})
        retrieval_k = assembly_k if context_assembly in {"ranked-snippets", "evidence-ledger"} else k
        recall_limit = max(retrieval_k, len(inst.get("haystack_session_ids", []) or []))
        r = srv.call("perseus_vault_recall", {"query": inst["question"], "mode": "hybrid",
                                      "category": qid, "limit": recall_limit, "trust_weight": 0,
                                      "min_decay": 0})
        wire = normalize_recall_response(r, limit=recall_limit)
        if wire["status"] != "complete":
            raise RuntimeError(f"recall unavailable or incomplete: {wire['status']}")
        items = stable_ranked_items(wire["items"], inst["question"])
        chosen = [str(it.get("key") or it.get("id")) for it in items[:retrieval_k]
                  if it.get("key") or it.get("id")]
        if shared:
            # The live shared-key row IS the latest gold session; surface it in
            # the context under that session's real id/date.
            chosen = [dated_gold[-1] if key == SHARED_FACT_KEY else key for key in chosen]
        if context_assembly == "ranked-snippets":
            return assemble_ranked_snippets(
                inst, chosen, budget_tokens=context_budget,
                max_windows_per_session=assembly_windows,
                guidance=context_guidance
            )[:2]
        if context_assembly == "evidence-ledger":
            return assemble_evidence_ledger(
                inst["question"], inst["haystack_session_ids"],
                inst["haystack_sessions"], inst.get("haystack_dates") or [],
                chosen, budget_tokens=ledger_budget
            )[:2]
        if context_assembly == "assistant-recall":
            return assemble_assistant_recall_ledger(
                inst["question"], inst["haystack_session_ids"],
                inst["haystack_sessions"], inst.get("haystack_dates") or [],
                chosen, budget_tokens=ledger_budget
            )[:2]
    else:
        raise ValueError(system)

    blocks = []
    for sid in chosen:
        if sid in by_id:
            turns, d = by_id[sid]
            hdr = f"[session {sid}" + (f" | {d}" if d else "") + "]"
            blocks.append(f"{hdr}\n{session_text(turns)}")
    ctx = "\n\n".join(blocks) or "(no prior conversation history is available)"
    return ctx, [s for s in chosen if s in by_id]


def _report_budget_tokens(args):
    if args.context_assembly in ("evidence-ledger", "assistant-recall"):
        return args.ledger_budget
    return args.context_budget


def price_for(model):
    return PRICING.get(model, FALLBACK_PRICE)


def estimate_cost(data, systems, k, model, judge, context_assembly="full",
                  assembly_k=20, context_budget=32768, ledger_budget=12000):
    """Rough upfront USD estimate from dataset shape (4-chars/token heuristic).

    Ranked-snippet estimates use the configured candidate depth capped by the
    answer-facing context budget, rather than silently pricing the old top-k
    baseline. The estimate remains a bound/heuristic; provider usage is the
    only quotable cost evidence.
    """
    sample = data[:min(len(data), 20)]
    sess_toks, sess_counts = [], []
    for inst in sample:
        sess_counts.append(len(inst["haystack_sessions"]))
        for turns in inst["haystack_sessions"][:10]:
            sess_toks.append(est_tokens(session_text(turns)))
    avg_sess = sum(sess_toks) / max(1, len(sess_toks))
    avg_n = sum(sess_counts) / max(1, len(sess_counts))

    ranked_ctx = min(context_budget, assembly_k * avg_sess)
    ledger_ctx = min(ledger_budget, 16000)
    if context_assembly == "ranked-snippets":
        vault_ctx = ranked_ctx
    elif context_assembly == "evidence-ledger":
        vault_ctx = ledger_ctx
    elif context_assembly == "assistant-recall":
        vault_ctx = ledger_ctx
    else:
        vault_ctx = k * avg_sess
    ctx_per_system = {"perseus-vault": vault_ctx, "fullcontext": avg_n * avg_sess,
                      "oracle": 2 * avg_sess, "stateless": 12}
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


def _qa_dataset_sha256(data):
    """Commit the exact filtered question/answer dataset without retaining it."""
    return replay_sha256_text(replay_stable_json(data))


def main():
    ap = argparse.ArgumentParser(description="LongMemEval end-to-end QA accuracy (pinned answerer + judge)")
    ap.add_argument("--data", default=None,
                    help="Path to longmemeval_<split>_cleaned.json (default: ./longmemeval_<split>_cleaned.json)")
    ap.add_argument("--split", default="s", choices=["s", "m"],
                    help="LongMemEval split; 's' (500 instances) is what Zep reports on")
    ap.add_argument("--systems", nargs="+", default=["perseus-vault"],
                    choices=["stateless", "fullcontext", "perseus-vault", "oracle"],
                    help="Run every system through the SAME model (default: perseus_vault only). "
                         "stateless = no history at all (arm 0); fullcontext = whole "
                         "haystack stuffed (no-memory-layer baseline); perseus_vault = the product; "
                         "oracle = gold evidence only (upper bound)")
    ap.add_argument("--model", default=DEFAULT_ANSWERER, help=f"Answerer model id (default {DEFAULT_ANSWERER})")
    ap.add_argument("--judge", default=DEFAULT_JUDGE, help=f"Judge model id (default {DEFAULT_JUDGE})")
    ap.add_argument("--k", type=int, default=10, help="Sessions retrieved for the perseus_vault system (default 10)")
    ap.add_argument("--context-assembly", choices=["full", "ranked-snippets", "evidence-ledger", "assistant-recall"], default="full",
                    help="Answer-facing context projection; default full preserves the baseline, "
                         "ranked-snippets retrieves a deeper pool and packs conversational pairs")
    ap.add_argument("--assembly-k", type=int, default=20,
                    help="Candidate retrieval depth for ranked-snippets (default 20)")
    ap.add_argument("--context-budget", type=int, default=32768,
                    help="chars/4 context budget for ranked-snippets (default 32768)")
    ap.add_argument("--ledger-budget", type=int, default=12000,
                    help="chars/4 budget for evidence-ledger (default 12000; hard max 16000)")
    ap.add_argument("--assembly-windows", type=int, default=2,
                    help="Maximum non-overlapping turn pairs per session (default 2)")
    ap.add_argument("--context-guidance", choices=["none", "preference", "preference-structured", "evidence-structured"], default="none",
                    help="Explicit answer-facing guide for ranked-snippets; preference "
                         "privileges direct user statements over assistant suggestions; "
                         "preference-structured labels provenance; evidence-structured adds "
                         "dated fact/event evidence for cross-session and temporal questions")
    ap.add_argument("--limit", type=int, default=0, help="Only run the first N instances (0 = all; smoke tests)")
    ap.add_argument("--cot", action="store_true",
                    help="#579: use LongMemEval's OFFICIAL chain-of-thought answer prompt. "
                         "The complete answerer response is retained for the official "
                         "judge and hypothesis artifact. Recorded as answer_prompt="
                         "'official-cot' in the journal config and report so it is never "
                         "blended with a plain-prompt number.")
    ap.add_argument("--only-types", nargs="+", default=None, metavar="TYPE",
                    help="#579: restrict the run to these question_type categories (e.g. "
                         "single-session-preference multi-session temporal-reasoning) — for the "
                         "weak-category slice experiments. Pinned into the journal config so a "
                         "--resume can't silently mix a slice with a full run.")
    ap.add_argument("--ku-shared-key", action="store_true",
                    help="PRODUCT-shape ingest for version-bearing questions (perseus_vault arm): gold "
                         "fact-version sessions share one key with valid_from = session date "
                         "(latest-wins; stale versions live in entity_history). See INGEST_590.md.")
    ap.add_argument("--bin", default=None, help="perseus-vault binary (else auto-located / PERSEUS_VAULT_BIN)")
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
    ap.add_argument("--max-retries", type=int, default=12,
                    help="Maximum provider attempts per answer/judge call (default 12; "
                         "use 1 for a no-retry canary).")
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
    # #579: category slice filter, applied BEFORE --limit so `--only-types X
    # --limit 50` means "first 50 of type X", not "of the first 50, those of
    # type X".
    if args.only_types:
        only = set(args.only_types)
        full = [inst for inst in full if inst.get("question_type") in only]
        if not full:
            sys.exit(f"error: --only-types {sorted(only)} matched no instances "
                     f"in {data_path.name}")
    data = full[: args.limit] if args.limit else full

    live = not (args.mock_llm or args.dry_run)
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = get_api_key() if live else ""

    if not args.dry_run:
        cost, toks, detail = estimate_cost(
            data, args.systems, args.k, args.model, args.judge,
            context_assembly=args.context_assembly,
            assembly_k=args.assembly_k, context_budget=args.context_budget,
            ledger_budget=args.ledger_budget
        )
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

    need_vault = "perseus-vault" in args.systems
    binary = find_binary(args.bin) if need_vault else None
    bin_ver = binary_version(binary) if binary else "n/a"
    db = str(Path(os.environ.get("TMPDIR") or "/tmp") / "perseus_vault-qa.db")


    def wipe():
        for ext in ("", "-wal", "-shm", "-journal"):
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
    # than silently blending two configurations. The content-hashed report is still
    # produced ONLY at completion over the full verdict set: a partial journal
    # is never marked complete or quotable.
    model_tag = ("mock" if args.mock_llm else args.model).replace("/", "_")
    journal_path = Path(args.journal) if args.journal else \
        Path(args.outdir) / f"qa_progress-{args.split}-{model_tag}.jsonl"
    run_config = {"split": args.split, "n": len(data),
                  "dataset_sha256": _qa_dataset_sha256(data),
                  "systems": sorted(args.systems),
                  "model": "mock" if args.mock_llm else args.model,
                  "judge": "mock" if args.mock_llm else args.judge,
                  "k": args.k,
                  "answer_prompt": "official-cot" if args.cot else "plain",
                  "hypothesis_mode": HYPOTHESIS_MODE,
                  "only_types": sorted(args.only_types) if args.only_types else None,
                  "ku_shared_key": args.ku_shared_key,
                  "context_assembly": args.context_assembly,
                  "assembly_k": args.assembly_k,
                  "context_budget": args.context_budget,
                  "ledger_budget": args.ledger_budget,
                  "assembly_windows": args.assembly_windows,
                  "context_guidance": args.context_guidance,
                  "max_retries": args.max_retries}
    preflight_by_question = {}
    done = {}
    journal = None
    if not args.dry_run:
        resume_ok = False
        if args.resume and journal_path.exists():
            lines = [json.loads(ln) for ln in
                     journal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if not lines or not isinstance(lines[0], dict) or set(lines[0]) != {"_config"}:
                sys.exit(f"error: --resume: {journal_path} has no config header — "
                         "not a progress journal (delete it or pass --journal).")
            if lines[0]["_config"] != run_config:
                sys.exit("error: --resume config mismatch:\n"
                         f"  journal: {lines[0]['_config']}\n  current: {run_config}\n"
                         "Delete the journal (or pass --journal) to start fresh.")
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
                    validate_qa_resume_record(
                        rec,
                        instance=instance,
                        systems=args.systems,
                        require_preflight=need_vault,
                        preflight_runtime=(
                            {
                                "binary": binary,
                                "db_path": db,
                                "repo_root": str(REPO),
                                "dataset": {"question_id": qid, "instance": instance},
                                "config": {**run_config, "question_id": qid},
                            }
                            if need_vault and qid not in preflight_seen
                            else None
                        ),
                    )
                    record_key = (qid, rec["system"])
                    if record_key in seen:
                        raise ValueError("duplicate QA journal record")
                    seen.add(record_key)
                    if need_vault:
                        previous = preflight_seen.get(qid)
                        if previous is not None and previous != rec["preflight"]:
                            raise ValueError("inconsistent QA preflight bindings")
                        if previous is None:
                            preflight_seen[qid] = rec["preflight"]
                except Exception:
                    sys.exit("error: --resume journal record failed validation")
                if rec["error"] is None:
                    loaded.append(rec)
            by_question = {}
            for rec in loaded:
                by_question.setdefault(rec["question_id"], []).append(rec)
            complete_questions = {
                qid for qid, rows in by_question.items()
                if {row["system"] for row in rows} == set(args.systems)
            }
            for rec in loaded:
                qid = rec["question_id"]
                if qid not in complete_questions:
                    continue
                if need_vault:
                    previous = preflight_by_question.get(qid)
                    if previous is not None and previous != rec["preflight"]:
                        sys.exit("error: --resume question has inconsistent preflight bindings")
                    preflight_by_question[qid] = rec["preflight"]
                done[(qid, rec["system"])] = rec
            resume_ok = True
            print(f"  resume: {len(done)} judged answers reloaded from "
                  f"{journal_path.name}; errored/unfinished questions will run.")
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal = open(journal_path, "a" if resume_ok else "w", encoding="utf-8")
        if not resume_ok:
            write_checkpoint(journal, {"_config": run_config})
        # Seed the accumulators from the reloaded verdicts so the final report
        # covers the WHOLE run, not just this process's share.
        for rec in done.values():
            tok[rec["system"]] += rec.get("tokens_est", 0)
            nsess[rec["system"]] += rec.get("sessions", 0)
            hyps[rec["system"]].append({"question_id": rec["question_id"],
                                        "hypothesis": rec.get("hypothesis", "")})
            verdicts.append({k: rec.get(k) for k in
                             ("question_id", "question_type", "system",
                              "abstention", "correct", "error", "judge_raw",
                              "ans_usage", "judge_usage")})


    def record(rec, hypothesis, tokens_est, sessions):
        """Append a verdict to memory AND the crash-safe journal."""
        verdicts.append(rec)
        if journal:
            checkpoint = {**rec, "hypothesis": hypothesis,
                          "tokens_est": tokens_est,
                          "sessions": sessions,
                          **({"preflight": preflight_by_question.get(rec["question_id"])}
                             if need_vault else {})}
            write_checkpoint(journal, _seal_qa_journal_record(checkpoint))

    for idx, inst in enumerate(data):
        qid = inst["question_id"]
        qtype = inst.get("question_type", "unknown")
        is_abs = qid.endswith("_abs")
        # #518: fully-judged instances skip even the (expensive) re-ingest.
        if not args.dry_run and all((qid, s) in done for s in args.systems):
            continue
        srv = None
        if need_vault:
            wipe()
            assert binary is not None
            preflight_by_question[qid] = prepare_recall_preflight(
                binary=binary,
                db_path=db,
                dataset={"question_id": qid, "instance": inst},
                config={**run_config, "question_id": qid},
                repo_root=str(REPO),
            )
            srv = PerseusVaultServer(binary, db)
        try:
            for system in args.systems:
                if (qid, system) in done:
                    continue
                ctx, chosen = build_context(
                    system, inst, srv, qid, args.k,
                    ku_shared=args.ku_shared_key,
                    context_assembly=args.context_assembly,
                    assembly_k=args.assembly_k,
                    context_budget=args.context_budget,
                    assembly_windows=args.assembly_windows,
                    context_guidance=args.context_guidance,
                    ledger_budget=args.ledger_budget,
                )
                answer_tmpl = ANSWER_PROMPT_COT if args.cot else ANSWER_PROMPT
                if args.cot:
                    # Keep the official positional template verbatim; the upstream
                    # runner fills context, date, and question in this order.
                    prompt = answer_tmpl.format(
                        ctx, inst.get("question_date", "unknown"), inst["question"]
                    )
                else:
                    prompt = answer_tmpl.format(
                        context=ctx,
                        question=inst["question"],
                        question_date=inst.get("question_date", "unknown"),
                    )
                tok[system] += est_tokens(prompt)
                nsess[system] += len(chosen)
                if args.dry_run:
                    hyps[system].append({"question_id": qid, "hypothesis": ""})
                    continue

                q_tokens = est_tokens(prompt)
                a_usage = {}
                if args.mock_llm:
                    raw_ans = mock_answer(inst, idx)
                else:
                    try:
                        # #579: CoT needs completion room to reason (1200 tok);
                        # the plain prompt keeps the provider default.
                        raw_ans, a_usage = call_llm(
                            base_url, api_key, args.model, prompt, budget,
                            max_retries=args.max_retries,
                            max_tokens=1200 if args.cot else None
                        )
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
                                "judge_raw": None, "ans_usage": None,
                                "judge_usage": None}, "", q_tokens, len(chosen))
                        continue
                # Official LongMemEval judging receives the complete response.
                # Do not replace it with extract_cot_answer(raw_ans); that tail-only
                # behavior caused the invalid 74.3% refresh.
                ans = hypothesis_for_judge(raw_ans, cot=args.cot)
                hyps[system].append({"question_id": qid, "hypothesis": ans})
                jp = get_anscheck_prompt(qtype, inst["question"], inst["answer"],
                                         ans or "(no answer)", abstention=is_abs)
                j_usage = {}
                if args.mock_llm:
                    jraw = mock_judge(inst, ans)
                else:
                    try:
                        jraw, j_usage = call_llm(
                            base_url, api_key, args.judge, jp, budget,
                            max_retries=args.max_retries
                        )
                    except Exception as e:
                        print(f"  !! JUDGE_ERROR on {qid}/{system} (excluded from accuracy): {e}",
                              file=sys.stderr)
                        record({"question_id": qid, "question_type": qtype,
                                "system": system, "abstention": is_abs,
                                "correct": None, "error": "judge_error",
                                "judge_raw": None, "ans_usage": a_usage or None,
                                "judge_usage": None}, ans, q_tokens, len(chosen))
                        continue
                correct = jraw.strip().lower().startswith("yes")
                record({"question_id": qid, "question_type": qtype, "system": system,
                        "abstention": is_abs, "correct": correct, "error": None,
                        "judge_raw": jraw.strip()[:40], "ans_usage": a_usage or None,
                        "judge_usage": j_usage or None}, ans, q_tokens, len(chosen))
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
    if need_vault:
        wipe()
    if journal:
        journal.close()

    n = len(data)
    # Hypotheses files in LongMemEval's official format, so their evaluate_qa.py
    # can independently cross-check our judge. Skipped in dry-run (empty).
    # (On a resumed run, reloaded answers come first — evaluate_qa.py keys on
    # question_id, so order is immaterial.)
    if not args.dry_run:
        Path(args.outdir).mkdir(parents=True, exist_ok=True)
        for system in args.systems:
            out = Path(args.outdir) / hypothesis_artifact_name(
                system, model_tag, cot=args.cot
            )
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
    if "fullcontext" in args.systems and "perseus-vault" in args.systems and tok["perseus-vault"]:
        print(f"\nvault feeds {tok['fullcontext'] / tok['perseus-vault']:.1f}x fewer tokens to the LLM "
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
            # API-billed tokens (ground truth for CPST cost accounting).
            # Zero when --mock-llm / --dry-run or resumed from a pre-usage journal.
            "api_usage_tokens": {
                "answer_prompt": sum((v.get("ans_usage") or {}).get("prompt_tokens", 0) for v in vs),
                "answer_completion": sum((v.get("ans_usage") or {}).get("completion_tokens", 0) for v in vs),
                "judge_prompt": sum((v.get("judge_usage") or {}).get("prompt_tokens", 0) for v in vs),
                "judge_completion": sum((v.get("judge_usage") or {}).get("completion_tokens", 0) for v in vs),
            },
        }
        if answer_errors or judge_errors:
            print(f"  !! {system}: {answer_errors} answer_error(s) + {judge_errors} judge_error(s) "
                  f"EXCLUDED from the accuracy denominator ({len(graded)}/{len(vs)} graded). "
                  "Re-run those questions (lower --tpm or higher tier) before publishing.",
                  file=sys.stderr)

    # Content hash over the verdict set (same convention as other content-hashed reports).
    sig_payload = json.dumps({
        "benchmark": "perseus-vault-longmemeval-qa",
        "split": f"longmemeval_{args.split}", "n": n,
        "answerer": "mock" if args.mock_llm else args.model,
        "judge": "mock" if args.mock_llm else args.judge,
        "answer_prompt": "official-cot" if args.cot else "plain",
        "hypothesis_mode": HYPOTHESIS_MODE,
        "only_types": sorted(args.only_types) if args.only_types else None,
        "ku_shared_key": args.ku_shared_key,
        "context_assembly": args.context_assembly,
        "assembly_k": args.assembly_k,
        "context_budget": args.context_budget,
        "ledger_budget": args.ledger_budget,
        "assembly_windows": args.assembly_windows,
        "context_guidance": args.context_guidance,
        "max_retries": args.max_retries,
        "preflight": {"questions": {key: preflight_by_question[key] for key in sorted(preflight_by_question)}},
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
        # #579: which official answer prompt produced these numbers. NEVER blend
        # a CoT accuracy with a plain-prompt one — the scoreboard must state this
        # next to any competitor row.
        "answer_prompt": "official-cot" if args.cot else "plain",
        "hypothesis_mode": HYPOTHESIS_MODE,
        "only_types": sorted(args.only_types) if args.only_types else None,
        # #590: ingest shape for the perseus_vault arm. NEVER compare a ku-shared-key
        # accuracy against a benchmark-shape one without labeling both.
        "ingest_shape": "ku-shared-key (product)" if args.ku_shared_key else "unique-key-per-session (benchmark)",
        "answerer_model": "mock" if args.mock_llm else args.model,
        "judge_model": "mock" if args.mock_llm else args.judge,
        "temperature": 0,
        "max_retries": args.max_retries,
        "retrieval": {"mode": "hybrid", "k": args.k, "embedding": "bundled-onnx"},
        "context_assembly": {"mode": args.context_assembly, "assembly_k": args.assembly_k,
                             "budget_tokens": _report_budget_tokens(args),
                             "context_budget": args.context_budget,
                             "ledger_budget": args.ledger_budget,
                             "windows_per_session": args.assembly_windows,
                             "guidance": args.context_guidance},
        "systems": systems_report,
        "commit": git_commit(),
        "binary": Path(binary).name if binary else None,
        "preflight": {"questions": {key: preflight_by_question[key] for key in sorted(preflight_by_question)}},
        "response_schema": RECALL_WIRE_SCHEMA_VERSION if preflight_by_question else None,
        "binary_version": bin_ver,
        "platform": platform.platform(),
        "hardware": {"machine": platform.machine(), "processor": platform.processor(),
                     "cpu_count": os.cpu_count()},
        "elapsed_secs": round(time.time() - t0, 1),
        "tpm_budget": args.tpm if live else None,
        "content_hash_sha256": signature,
        "per_question": [{"question_id": v["question_id"], "question_type": v["question_type"],
                          "system": v["system"], "correct": v["correct"],
                          "error": v["error"], "ans_usage": v.get("ans_usage"),
                          "judge_usage": v.get("judge_usage")} for v in verdicts],
    }
    report_path = Path(args.out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

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
