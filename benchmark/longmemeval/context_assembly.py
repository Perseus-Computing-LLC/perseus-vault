"""Gold-independent context assembly for the LongMemEval answer-facing arm.

The default QA path sends complete retrieved sessions unchanged. This module is
an opt-in projection: it retrieves a deeper ranked pool, keeps a bounded number
of non-overlapping conversational turn pairs per session, and packs them in
retrieval order under a deterministic character/4 token budget. It never reads
answer_session_ids or the gold answer.
"""
from __future__ import annotations

import re
from typing import Any


_STOPWORDS = frozenset(
    "a an and are as at be been being before between but by can could did do does "
    "for from had has have how i if in into is it me more most my of on or our "
    "should than that the their them there they this to us was were what when "
    "where which who why will with would you your".split()
)

_PREFERENCE_GUIDANCE = (
    "[Preference evidence guide]\n"
    "Use direct user statements as the source of personal preferences, past "
    "experiences, plans, and constraints. Treat assistant suggestions or generic "
    "examples as supporting context, not as user preferences unless the user "
    "confirms them. For recommendation questions, tailor the response to the "
    "most specific user-stated details and distinguish a current plan from a "
    "general preference."
)


def _tokens(text: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def _turn_text(turn: object) -> str:
    if not isinstance(turn, dict):
        return str(turn)
    return f"{turn.get('role', '')}: {turn.get('content', '')}"


def _date_header(session_id: str, date: object, start: int, end: int) -> str:
    suffix = f" | {date}" if date else ""
    return f"[session {session_id}{suffix} | turns {start + 1}-{end}]"


def assemble_ranked_snippets(
    inst: dict,
    ranked_ids: list[str],
    *,
    budget_tokens: int = 32768,
    max_windows_per_session: int = 2,
    guidance: str = "none",
) -> tuple[str, list[str], dict[str, Any]]:
    """Assemble a deterministic, bounded context projection.

    ``ranked_ids`` is the producer-ranked candidate list. Candidate sessions
    retain their producer order; within each session, non-overlapping pairs are
    scored by query-token overlap and the strongest pairs are retained. The
    output contains at most ``max_windows_per_session`` pairs per session and
    never exceeds ``budget_tokens`` under the same chars/4 estimate used by the
    fused recall contract.

    Returns ``(context_text, selected_session_ids, telemetry)``. The selected
    IDs are unique and ordered by the first selected window's producer rank.
    Unknown/duplicate producer IDs are ignored so a malformed response cannot
    duplicate context silently.
    """
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    if max_windows_per_session <= 0:
        raise ValueError("max_windows_per_session must be positive")
    if guidance not in {"none", "preference"}:
        raise ValueError(f"unknown context guidance: {guidance}")
    guidance_text = _PREFERENCE_GUIDANCE if guidance == "preference" else ""
    guidance_cost = max(1, len(guidance_text) // 4) if guidance_text else 0
    selection_budget = budget_tokens - guidance_cost

    session_ids = list(inst.get("haystack_session_ids", []) or [])
    sessions = list(inst.get("haystack_sessions", []) or [])
    dates = list(inst.get("haystack_dates", []) or [])
    if len(dates) < len(session_ids):
        dates.extend([None] * (len(session_ids) - len(dates)))
    by_id = {
        sid: (turns, dates[index])
        for index, (sid, turns) in enumerate(zip(session_ids, sessions))
    }
    query_terms = _tokens(inst.get("question", ""))

    unique_ranked: list[str] = []
    seen_ids: set[str] = set()
    for sid in ranked_ids:
        if sid in by_id and sid not in seen_ids:
            unique_ranked.append(sid)
            seen_ids.add(sid)

    candidates: list[dict[str, Any]] = []
    for rank, sid in enumerate(unique_ranked):
        turns, date = by_id[sid]
        local: list[dict[str, Any]] = []
        # LongMemEval sessions alternate user/assistant turns. Pairing adjacent
        # turns preserves the question/answer unit without overlapping copies.
        for start in range(0, len(turns), 2):
            end = min(len(turns), start + 2)
            body = "\n".join(_turn_text(turn) for turn in turns[start:end])
            local.append(
                {
                    "rank": rank,
                    "sid": sid,
                    "date": date,
                    "start": start,
                    "end": end,
                    "body": body,
                    "overlap": len(query_terms & _tokens(body)),
                    "cost": max(1, len(body) // 4),
                }
            )
        local.sort(key=lambda item: (-int(item["overlap"]), int(item["start"])))
        candidates.extend(local[:max_windows_per_session])

    candidates.sort(
        key=lambda item: (
            int(item["rank"]),
            -int(item["overlap"]),
            int(item["start"]),
        )
    )

    selected: list[dict[str, Any]] = []
    used = 0
    skipped = 0
    clipped = 0
    for candidate in candidates:
        if selection_budget <= 0:
            skipped += 1
            continue
        cost = int(candidate["cost"])
        body = str(candidate["body"])
        if selected and used + cost > selection_budget:
            skipped += 1
            continue
        if not selected and cost > selection_budget:
            # A single pathological turn pair must not bypass the budget. Keep
            # the prefix and mark the loss explicitly in telemetry.
            body = body[: selection_budget * 4]
            cost = max(1, len(body) // 4)
            cost = min(cost, selection_budget)
            candidate = {**candidate, "body": body, "cost": cost}
            clipped += 1
        selected.append(candidate)
        used += cost

    blocks: list[str] = []
    selected_ids: list[str] = []
    selected_seen: set[str] = set()
    for candidate in selected:
        sid = str(candidate["sid"])
        if sid not in selected_seen:
            selected_ids.append(sid)
            selected_seen.add(sid)
        blocks.append(
            f"{_date_header(sid, candidate['date'], int(candidate['start']), int(candidate['end']))}\n"
            f"{candidate['body']}"
        )

    context_body = "\n\n".join(blocks) or "(no prior conversation history is available)"
    context = f"{guidance_text}\n\n{context_body}" if guidance_text else context_body
    telemetry = {
        "candidate_sessions": len(unique_ranked),
        "candidate_windows": len(candidates),
        "selected_windows": len(selected),
        "selected_sessions": len(selected_ids),
        "estimated_tokens": min(used + guidance_cost, budget_tokens),
        "skipped_windows": skipped,
        "clipped_windows": clipped,
        "max_windows_per_session": max_windows_per_session,
        "budget_tokens": budget_tokens,
        "guidance_applied": guidance,
    }
    return context, selected_ids, telemetry
