#!/usr/bin/env python3
"""Provider-free paired gate for the LongMemEval evidence-ledger arm.

This command is the free, preregistered half of issue #1109.  It never imports
an answerer or judge client.  Retrieval can be replayed from a pinned offline
artifact, or executed with a pinned Vault binary; in both modes the baseline
and candidate receive the same retrieved session IDs.  Gold-only fields are
read only by the evaluator after source assembly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from context_assembly import assemble_evidence_ledger, assemble_ranked_snippets  # noqa: E402


EXPECTED_STRATA = {
    "both_correct": 18,
    "candidate_gain_over_fullcontext": 15,
    "candidate_regression_vs_fullcontext": 6,
    "both_wrong_or_answer_limited": 24,
}
EXPECTED_CASES = sum(EXPECTED_STRATA.values())
HARD_MAX_TOKENS = 16_000
DEFAULT_TARGET_P95 = 12_000


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _words(text: object) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) > 2 or token.isdigit()
    }


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, (len(ordered) * 95 + 99) // 100 - 1)
    return ordered[index]


def _source_view(inst: dict[str, Any]) -> dict[str, Any]:
    """Project only source material into either arm."""
    return {
        "question": inst.get("question", ""),
        "haystack_session_ids": list(inst.get("haystack_session_ids", []) or []),
        "haystack_sessions": list(inst.get("haystack_sessions", []) or []),
        "haystack_dates": list(inst.get("haystack_dates", []) or []),
    }


def _answer_token_proxy(target: object, context: str) -> float | None:
    target_words = _words(target)
    if not target_words:
        return None
    return round(len(target_words & _words(context)) / len(target_words), 4)


def _session_records(inst: dict[str, Any], selected: list[str]) -> list[dict[str, Any]]:
    dates = dict(zip(inst.get("haystack_session_ids", []), inst.get("haystack_dates", [])))
    return [{"session_id": sid, "date": dates.get(sid)} for sid in selected]


def _dated_evidence_blocks(context: str) -> list[str]:
    return [
        line for line in context.splitlines()
        if line.startswith("[rank=") or line.startswith("- [")
    ]


def _token_sequence(text: object) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) > 2 or token.isdigit()
    ]


def _is_token_subsequence(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return True
    position = 0
    for token in haystack:
        if token == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


def _source_token_check(context: str, inst: dict[str, Any]) -> dict[str, Any]:
    """Verify every emitted statement is an extract from its source turn."""
    by_id = {
        sid: turns
        for sid, turns in zip(
            inst.get("haystack_session_ids", []),
            inst.get("haystack_sessions", []),
        )
    }
    statements = [line for line in context.splitlines() if line.startswith("- [")]
    matched = 0
    preserved_tokens = 0
    total_tokens = 0
    failures: list[str] = []
    for line in statements:
        if " :: " not in line:
            failures.append(line)
            continue
        meta, text = line.split(" :: ", 1)
        match = re.search(r"sources=([^ ]+)", meta)
        refs = match.group(1).split(",") if match else []
        found = False
        for ref in refs:
            sid, marker, turn_text = ref.partition(":turn=")
            if not marker or not turn_text.isdigit():
                continue
            turns = by_id.get(sid, [])
            index = int(turn_text) - 1
            if 0 <= index < len(turns):
                content = str(turns[index].get("content", ""))
                normalized_text = " ".join(text.split())
                normalized_content = " ".join(content.split())
                if (normalized_text in normalized_content or
                        _is_token_subsequence(_token_sequence(text), _token_sequence(content))):
                    found = True
                    break
        source_tokens = _words(text)
        total_tokens += len(source_tokens)
        if found:
            matched += 1
            preserved_tokens += len(source_tokens)
        else:
            failures.append(line)
    count = len(statements)
    return {
        "statements": count,
        "matched_statements": matched,
        "source_token_preservation": matched == count,
        "source_token_fraction": round(preserved_tokens / total_tokens, 4) if total_tokens else 1.0,
        "failures": failures[:5],
    }


def _arm_record(
    name: str,
    context: str,
    selected: list[str],
    inst: dict[str, Any],
    *,
    oracle_hypothesis: object,
) -> dict[str, Any]:
    return {
        "name": name,
        "selected_sessions": _session_records(inst, selected),
        "selected_session_count": len(selected),
        "all_gold": bool(inst.get("answer_session_ids")) and
        set(inst.get("answer_session_ids", [])) <= set(selected),
        "context_tokens_est": (len(context) + 3) // 4,
        "answer_token_proxy": _answer_token_proxy(inst.get("answer"), context),
        "oracle_hypothesis_token_proxy": _answer_token_proxy(oracle_hypothesis, context),
        "dated_evidence_blocks": _dated_evidence_blocks(context),
        "context_sha256": hashlib.sha256(context.encode("utf-8")).hexdigest(),
    }


def evaluate_case(
    inst: dict[str, Any],
    causal_row: dict[str, Any],
    focus_row: dict[str, Any],
    ranked_ids: list[str],
    *,
    budget_tokens: int = 12_000,
    baseline_budget: int = 32_768,
    baseline_guidance: str = "preference",
) -> dict[str, Any]:
    """Evaluate both context arms over one fixed case, with no model calls."""
    source = _source_view(inst)
    baseline_context, baseline_selected, _baseline_telemetry = assemble_ranked_snippets(
        source,
        ranked_ids,
        budget_tokens=baseline_budget,
        max_windows_per_session=2,
        guidance=baseline_guidance,
    )
    candidate_args = (
        source["question"], source["haystack_session_ids"],
        source["haystack_sessions"], source["haystack_dates"], ranked_ids,
    )
    candidate_first = assemble_evidence_ledger(*candidate_args, budget_tokens=budget_tokens)
    candidate_second = assemble_evidence_ledger(*candidate_args, budget_tokens=budget_tokens)
    candidate_context, candidate_selected, candidate_telemetry = candidate_first
    source_check = _source_token_check(candidate_context, inst)
    deterministic = candidate_first == candidate_second
    candidate_tokens = candidate_telemetry["estimated_tokens"]
    return {
        "question_id": inst["question_id"],
        "question_type": inst.get("question_type", "unknown"),
        "stratum": causal_row["bucket"],
        "provider_calls": 0,
        "judge_calls": 0,
        "baseline": _arm_record(
            "ranked-snippets", baseline_context, baseline_selected, inst,
            oracle_hypothesis=focus_row.get("oracle_hypothesis"),
        ),
        "candidate": {
            **_arm_record(
                "evidence-ledger", candidate_context, candidate_selected, inst,
                oracle_hypothesis=focus_row.get("oracle_hypothesis"),
            ),
            "telemetry": candidate_telemetry,
        },
        "checks": {
            "deterministic": deterministic,
            "budget_ok": candidate_tokens <= budget_tokens and candidate_tokens <= HARD_MAX_TOKENS,
            "source_token_preservation": source_check["source_token_preservation"],
            "source_token_check": source_check,
            "no_provider_or_judge_calls": candidate_telemetry.get("provider_calls") == 0,
            "gold_session_inclusion_not_lower": (
                bool(inst.get("answer_session_ids")) and
                (set(inst.get("answer_session_ids", [])) <= set(candidate_selected)) >=
                (set(inst.get("answer_session_ids", [])) <= set(baseline_selected))
            ),
        },
    }


def validate_causal_rows(rows: list[dict[str, Any]]) -> bool:
    if len(rows) != EXPECTED_CASES:
        raise ValueError(f"causal ledger must contain exactly {EXPECTED_CASES} rows")
    ids = [row.get("question_id") for row in rows]
    if any(not item for item in ids) or len(set(ids)) != EXPECTED_CASES:
        raise ValueError("causal ledger question IDs must be unique and non-empty")
    counts = Counter(row.get("bucket") for row in rows)
    if dict(counts) != EXPECTED_STRATA:
        raise ValueError(f"causal strata mismatch: expected {EXPECTED_STRATA}, got {dict(counts)}")
    for row in rows:
        if not row.get("question_type"):
            raise ValueError("causal rows require question_type")
        if not isinstance(row.get("candidate_all_gold"), bool):
            raise ValueError("causal rows require boolean candidate_all_gold")
    return True


def _load_replay(path: Path) -> tuple[dict[str, list[str]], dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("offline") or data.get("provider_calls") != 0 or data.get("judge_calls") != 0:
        raise ValueError("retrieval replay must be explicitly offline with zero provider/judge calls")
    if not str(data.get("schema", "")).startswith("longmemeval-focus-failure-audit/"):
        raise ValueError("unsupported retrieval replay schema")
    if not data.get("binary") or not data.get("binary_version"):
        raise ValueError("retrieval replay must identify the pinned binary and version")
    result: dict[str, list[str]] = {}
    for row in data.get("cases", []):
        qid = row.get("question_id")
        replay = row.get("replay", {})
        probe = (replay.get("ordering_probes", {}) or {}).get("baseline_top20", {})
        ids = probe.get("ids") if isinstance(probe, dict) else None
        if not isinstance(ids, list):
            ids = (replay.get("baseline", {}) or {}).get("ids", [])[:20]
        if qid and isinstance(ids, list):
            result[qid] = [str(item) for item in ids]
    if len(result) != EXPECTED_CASES:
        raise ValueError(f"retrieval replay must contain {EXPECTED_CASES} fixed cases")
    return result, {
        "path": str(path),
        "sha256": sha256_file(path),
        "schema": data.get("schema"),
        "binary": data.get("binary"),
        "binary_version": data.get("binary_version"),
    }


def _aggregate(records: list[dict[str, Any]], field: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ["baseline", "candidate"]:
        rows = [row[key] for row in records]
        values = [int(row["context_tokens_est"]) for row in rows]
        proxies = [row[field] for row in rows if row[field] is not None]
        out[key] = {
            "n": len(rows),
            "all_gold": sum(bool(row["all_gold"]) for row in rows),
            "mean_context_tokens_est": round(sum(values) / len(values)) if values else 0,
            "p95_context_tokens_est": _p95(values),
            "mean_proxy": round(sum(proxies) / len(proxies), 4) if proxies else None,
        }
    return out


def _stratum_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stratum in EXPECTED_STRATA:
        rows = [row for row in records if row["stratum"] == stratum]
        baseline = _aggregate(rows, "answer_token_proxy")
        candidate = _aggregate(rows, "oracle_hypothesis_token_proxy")
        regressions = [
            row["question_id"] for row in rows
            if row["baseline"]["oracle_hypothesis_token_proxy"] is not None
            and row["candidate"]["oracle_hypothesis_token_proxy"] is not None
            and row["candidate"]["oracle_hypothesis_token_proxy"] <
            row["baseline"]["oracle_hypothesis_token_proxy"]
        ]
        out[stratum] = {
            "n": len(rows),
            "answer_token_proxy": baseline,
            "oracle_hypothesis_token_proxy": candidate,
            "oracle_hypothesis_regression_count": len(regressions),
            "oracle_hypothesis_regression_question_ids": regressions,
        }
    return out


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    fixture_path = Path(args.data)
    causal_path = Path(args.causal_ledger)
    focus_path = Path(args.focus_audit)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    causal = json.loads(causal_path.read_text(encoding="utf-8"))
    focus = json.loads(focus_path.read_text(encoding="utf-8"))
    if not isinstance(fixture, list) or len(fixture) != EXPECTED_CASES:
        raise ValueError(f"fixture must contain exactly {EXPECTED_CASES} cases")
    if causal.get("schema") != "longmemeval-causal-decomposition/v1":
        raise ValueError("unsupported causal ledger schema")
    if not causal.get("offline") or causal.get("provider_calls") != 0 or causal.get("judge_calls") != 0:
        raise ValueError("causal ledger must be explicitly offline with zero provider/judge calls")
    if not str(focus.get("schema", "")).startswith("longmemeval-focus-failure-audit/"):
        raise ValueError("unsupported focus-audit schema")
    if not focus.get("offline") or focus.get("provider_calls") != 0 or focus.get("judge_calls") != 0:
        raise ValueError("focus audit must be explicitly offline with zero provider/judge calls")
    causal_rows = causal.get("rows", [])
    validate_causal_rows(causal_rows)
    causal_by_id = {row["question_id"]: row for row in causal_rows}
    fixture_by_id = {row["question_id"]: row for row in fixture}
    if set(causal_by_id) != set(fixture_by_id):
        raise ValueError("fixture and causal ledger IDs do not match")
    if any(causal_by_id[qid].get("question_type") != inst.get("question_type")
           for qid, inst in fixture_by_id.items()):
        raise ValueError("fixture and causal ledger question types do not match")
    focus_cases = focus.get("cases", [])
    focus_by_id = {row["question_id"]: row for row in focus_cases}
    if len(focus_cases) != EXPECTED_CASES or len(focus_by_id) != EXPECTED_CASES:
        raise ValueError("focus audit must contain one row for each fixed case")
    if set(focus_by_id) != set(causal_by_id):
        raise ValueError("focus audit and causal ledger IDs do not match")

    replay_info: dict[str, Any] = {"mode": "pinned-binary"}
    replay_ids: dict[str, list[str]] | None = None
    if args.retrieval_replay:
        replay_ids, replay_info = _load_replay(Path(args.retrieval_replay))
    elif not args.bin:
        raise ValueError("--bin is required when --retrieval-replay is absent")

    records: list[dict[str, Any]] = []
    server = None
    temporary = None
    if replay_ids is None:
        import qa as QA  # local harness import; no provider client
        temporary = tempfile.TemporaryDirectory(prefix="evidence-ledger-gate-")
        server = QA.PerseusVaultServer(args.bin, str(Path(temporary.name) / "vault.db"))
    try:
        for index, inst in enumerate(fixture, 1):
            qid = inst["question_id"]
            if replay_ids is not None:
                ranked_ids = replay_ids[qid]
            else:
                import qa as QA
                source = _source_view(inst)
                _baseline_context, ranked_ids = QA.build_context(
                    "perseus-vault", source, server, qid, 10,
                    context_assembly="ranked-snippets",
                    context_guidance=args.baseline_guidance,
                    assembly_k=args.candidate_k,
                    context_budget=args.baseline_budget,
                    assembly_windows=2,
                    ledger_budget=args.budget,
                )
            record = evaluate_case(
                inst, causal_by_id[qid], focus_by_id[qid], ranked_ids,
                budget_tokens=args.budget,
                baseline_budget=args.baseline_budget,
                baseline_guidance=args.baseline_guidance,
            )
            records.append(record)
            if index % 10 == 0 or index == len(fixture):
                print(f"FREE_GATE {index}/{len(fixture)}", flush=True)
    finally:
        if server is not None:
            server.close()
        if temporary is not None:
            temporary.cleanup()

    by_type: dict[str, Any] = {}
    for question_type in sorted({row["question_type"] for row in records}):
        by_type[question_type] = _stratum_report(
            [row for row in records if row["question_type"] == question_type]
        )
    by_stratum = _stratum_report(records)
    baseline_all = sum(bool(row["baseline"]["all_gold"]) for row in records)
    candidate_all = sum(bool(row["candidate"]["all_gold"]) for row in records)
    causal_baseline_match = all(
        bool(row["baseline"]["all_gold"]) == bool(causal_by_id[row["question_id"]].get("candidate_all_gold"))
        for row in records
    )
    per_type_nonloss = all(
        sum(bool(row["candidate"]["all_gold"]) for row in records if row["question_type"] == qt)
        >= sum(bool(row["baseline"]["all_gold"]) for row in records if row["question_type"] == qt)
        for qt in {row["question_type"] for row in records}
    )
    candidate_tokens = [row["candidate"]["context_tokens_est"] for row in records]
    acceptance = {
        "fixed_63_case_strata": True,
        "baseline_ranked_snippets_all_gold": baseline_all,
        "candidate_evidence_ledger_all_gold": candidate_all,
        "baseline_matches_causal_reference": causal_baseline_match,
        "baseline_matches_preregistered_56": baseline_all == args.expected_baseline_all_gold,
        "candidate_not_lower_overall": candidate_all >= baseline_all,
        "candidate_not_lower_by_type": per_type_nonloss,
        "candidate_p95_tokens_le_target": _p95(candidate_tokens) <= DEFAULT_TARGET_P95,
        "candidate_hard_max_tokens": max(candidate_tokens, default=0) <= HARD_MAX_TOKENS,
        "all_cases_deterministic": all(row["checks"]["deterministic"] for row in records),
        "all_cases_source_token_preserved": all(row["checks"]["source_token_preservation"] for row in records),
        "no_provider_or_judge_calls": all(
            row["provider_calls"] == 0 and row["judge_calls"] == 0
            for row in records
        ),
    }
    acceptance["free_gate_passed"] = all(acceptance.values())
    report = {
        "schema": "longmemeval-evidence-ledger-free-gate/v1",
        "offline": True,
        "provider_calls": 0,
        "judge_calls": 0,
        "paid_run_authorized": False,
        "paid_run_allowed": bool(acceptance["free_gate_passed"]),
        "preregistration": {
            "primary": "candidate recovery on the both-wrong/oracle-right stratum",
            "safety": "no regression on the both-correct stratum",
            "selection": "no loss of ranked-snippet gold-session inclusion",
            "claim_boundary": "mechanism-level free gate; not full-split QA efficacy",
        },
        "fixture": {
            "path": str(fixture_path),
            "sha256": sha256_file(fixture_path),
            "n": len(fixture),
            "question_ids_sha256": hashlib.sha256(
                "\n".join(row["question_id"] for row in fixture).encode("utf-8")
            ).hexdigest(),
        },
        "causal_ledger": {"path": str(causal_path), "sha256": sha256_file(causal_path)},
        "focus_audit": {"path": str(focus_path), "sha256": sha256_file(focus_path)},
        "retrieval": replay_info,
        "configuration": {
            "candidate_k": args.candidate_k,
            "baseline_budget_tokens": args.baseline_budget,
            "candidate_budget_tokens": args.budget,
            "baseline_guidance": args.baseline_guidance,
            "hard_max_tokens": HARD_MAX_TOKENS,
            "target_p95_tokens": DEFAULT_TARGET_P95,
        },
        "acceptance": acceptance,
        "by_stratum": by_stratum,
        "by_question_type": by_type,
        "cases": records,
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline preregistered evidence-ledger free gate")
    parser.add_argument("--data", required=True, help="fixed 63-case fixture")
    parser.add_argument("--causal-ledger", required=True, help="causal decomposition ledger")
    parser.add_argument("--focus-audit", required=True, help="offline focus audit with oracle hypotheses")
    parser.add_argument("--retrieval-replay", default=None, help="pinned offline replay; avoids re-running retrieval")
    parser.add_argument("--bin", default=None, help="pinned Vault binary when no replay is supplied")
    parser.add_argument("--out", required=True, help="free-gate JSON output")
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--baseline-budget", type=int, default=32768)
    parser.add_argument("--baseline-guidance", choices=["none", "preference"], default="preference")
    parser.add_argument("--budget", type=int, default=12000, help="candidate budget; hard max is 16000")
    parser.add_argument("--expected-baseline-all-gold", type=int, default=56)
    args = parser.parse_args(argv)
    if not 1 <= args.budget <= HARD_MAX_TOKENS:
        parser.error(f"--budget must be between 1 and {HARD_MAX_TOKENS}")
    if args.candidate_k <= 0 or args.baseline_budget <= 0:
        parser.error("candidate and baseline budgets/depth must be positive")
    try:
        report = run_gate(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"FREE_GATE_ERROR: {exc}", file=sys.stderr)
        return 2
    candidate_tokens = [case["candidate"]["context_tokens_est"] for case in report["cases"]]
    print(json.dumps({"out": args.out, "free_gate_passed": report["acceptance"]["free_gate_passed"],
                      "baseline_all_gold": report["acceptance"]["baseline_ranked_snippets_all_gold"],
                      "candidate_all_gold": report["acceptance"]["candidate_evidence_ledger_all_gold"],
                      "candidate_p95": _p95(candidate_tokens)}, indent=2))
    return 0 if report["acceptance"]["free_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
