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

_PREFERENCE_STRUCTURED_GUIDANCE = (
    _PREFERENCE_GUIDANCE
    + "\n\n[Structured preference evidence guide]\n"
    + "Use [User-stated evidence] blocks as the source of personal preferences, "
    + "past experiences, plans, and constraints. Treat [Assistant context (not "
    + "user evidence)] blocks as context or suggestions, not as user facts unless "
    + "the user explicitly confirms them. Answer the question first, then tailor "
    + "recommendations to the most specific user-stated details."
)

_EVIDENCE_STRUCTURED_GUIDANCE = (
    "[Cross-session evidence guide]\n"
    "Treat each dated session as a separate evidence source. For multi-session "
    "questions, combine directly stated user facts across sessions before counting "
    "or summarizing. For temporal questions, use the displayed dates and explicit "
    "event dates; anchor relative expressions to the question date and only order "
    "events when the question asks for an order. Assistant suggestions are not user "
    "evidence."
)

_PREFERENCE_DURABLE_RE = re.compile(
    r"\b(?:prefer|enjoy|like|love|interested|experience|experienced|tried|"
    r"made|attended|visited|remember|nostalgic|feel|felt|found|liked|"
    r"dislike|avoid|would rather)\b",
    re.IGNORECASE,
)
_PREFERENCE_PLANNING_RE = re.compile(
    r"\b(?:want|need|plan|planning|considering|looking for|thinking|hope|"
    r"going to)\b",
    re.IGNORECASE,
)
# Kept as a broad diagnostic marker for offline reports; selection uses the
# narrower durable/planning split above.
_PREFERENCE_MARKER_RE = re.compile(
    r"\b(?:i|i'm|i've|i'd|my|me|we|our|enjoy|prefer|like|love|want|need|"
    r"have|had|am|interested|experience|experienced|tried|made|attended|"
    r"visited|remember|plan|planning|considering|looking|feel|felt|recently|"
    r"used|dislike|avoid|would rather|don't|do not|cannot|can't)\b",
    re.IGNORECASE,
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


def _structured_turn_text(turn: object) -> str:
    """Render role provenance without implying assistant text is user evidence."""
    if not isinstance(turn, dict):
        return f"[Assistant context (not user evidence)]\n{turn}"
    role = str(turn.get("role", "")).lower()
    content = str(turn.get("content", ""))
    if role == "user":
        return f"[User-stated evidence]\nuser: {content}"
    return f"[Assistant context (not user evidence)]\n{role or 'assistant'}: {content}"


def _preference_score(turns: object, query_terms: set[str]) -> float:
    """Score a pair for direct, user-authored preference evidence only."""
    if not isinstance(turns, list):
        return 0.0
    user_text = "\n".join(
        str(turn.get("content", ""))
        for turn in turns
        if isinstance(turn, dict) and str(turn.get("role", "")).lower() == "user"
    )
    if not user_text.strip():
        return 0.0
    terms = _tokens(user_text)
    durable = len(_PREFERENCE_DURABLE_RE.findall(user_text))
    planning = len(_PREFERENCE_PLANNING_RE.findall(user_text))
    declarative = 3.0 if "?" not in user_text else 0.0
    # User role is the hard provenance gate. Durable preferences, experiences,
    # and constraints beat generic first-person task planning; a declarative
    # statement is a useful tie-breaker. Query overlap remains a small tie-breaker
    # so the evidence window stays relevant to the question.
    return float(
        durable * 10
        + planning * 1.5
        + declarative
        + min(len(terms), 40) * 0.05
        + len(terms & query_terms) * 0.5
    )


_FACT_DATE_RE = re.compile(
    r"\b(?:\d{1,4}(?:[/-]\d{1,4})?|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|yesterday|today|tomorrow|"
    r"last week|two weeks|three weeks|next week|this month|past month)\b",
    re.IGNORECASE,
)
_FACT_EVENT_RE = re.compile(
    r"\b(?:attend(?:ed)?|participat(?:e|ed)|visit(?:ed)?|saw|bought|purchase|"
    r"started|finished|ended|offer|view(?:ed)?|event|workshop|lecture|conference|"
    r"game|race|triathlon|soccer|meeting|birthday|wedding|graduat(?:e|ed))\b",
    re.IGNORECASE,
)


def _fact_score(turns: object, query_terms: set[str]) -> float:
    """Score user-authored date/event facts for cross-session evidence overlays."""
    if not isinstance(turns, list):
        return 0.0
    user_text = "\n".join(
        str(turn.get("content", ""))
        for turn in turns
        if isinstance(turn, dict) and str(turn.get("role", "")).lower() == "user"
    )
    if not user_text.strip():
        return 0.0
    terms = _tokens(user_text)
    dates = len(_FACT_DATE_RE.findall(user_text))
    events = len(_FACT_EVENT_RE.findall(user_text))
    numbers = len(re.findall(r"\b\d+(?:[.,]\d+)?\b", user_text))
    declarative = 2.0 if "?" not in user_text else 0.0
    return float(
        dates * 4.0
        + numbers * 3.0
        + events * 2.0
        + declarative
        + min(len(terms), 40) * 0.03
        + len(terms & query_terms) * 0.25
    )


def _date_header(session_id: str, date: object, start: int, end: int) -> str:
    suffix = f" | {date}" if date else ""
    return f"[session {session_id}{suffix} | turns {start + 1}-{end}]"



_LEDGER_HARD_MAX_TOKENS = 16_000
_LEDGER_USER_LIMIT = 8
_LEDGER_ASSISTANT_LIMIT = 2
_LEDGER_COMPARISON_RE = re.compile(
    r"\b(?:more|less|than|before|after|earlier|later|first|last|then|instead|"
    r"versus|vs|same|different|increase|decrease|changed|change|from|to)\b",
    re.IGNORECASE,
)


def _ledger_estimate(text: str) -> int:
    """Use the conservative four-character estimate shared by the harness."""
    return (len(text) + 3) // 4


def _ledger_date_key(value: object) -> tuple[int, ...]:
    """Return a sortable key without depending on locale or a date library."""
    match = re.search(
        r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:\D+(\d{1,2}):(\d{2}))?",
        str(value or ""),
    )
    if not match:
        return (9_999, 12, 31, 23, 59)
    return tuple(int(part or 0) for part in match.groups())


def _ledger_clean_text(text: object) -> str:
    """Collapse layout whitespace while retaining the source wording."""
    return re.sub(r"\s+", " ", str(text)).strip(" \t\r\n-•")


def _ledger_sentences(text: object) -> list[str]:
    """Split source turns using punctuation and line boundaries only."""
    clean = _ledger_clean_text(text)
    if not clean:
        return []
    pieces = re.split(r"(?<=[.!?])\s+|\s*\|\s*", clean)
    return [piece.strip() for piece in pieces if piece.strip()]


def _ledger_key(text: str) -> tuple[str, ...]:
    """Keep numbers and dates in a normalized key so distinct versions survive."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return tuple(token for token in tokens if token not in _STOPWORDS or token.isdigit())


def _ledger_score(text: str, role: str, query_terms: set[str]) -> float:
    """Rank extractive statements using only query and source text."""
    terms = _tokens(text)
    dates = len(_FACT_DATE_RE.findall(text))
    events = len(_FACT_EVENT_RE.findall(text))
    numbers = len(re.findall(r"\b\d+(?:[.,]\d+)?\b", text))
    comparisons = len(_LEDGER_COMPARISON_RE.findall(text))
    role_bonus = 20.0 if role == "user" else 5.0
    return (
        role_bonus
        + len(terms & query_terms) * 8.0
        + dates * 6.0
        + numbers * 5.0
        + events * 3.0
        + comparisons * 3.0
    )


def _ledger_safe_label(value: object, fallback: str) -> str:
    """Make one-line provenance labels stable even for malformed input."""
    text = _ledger_clean_text(value)
    return text or fallback


def assemble_evidence_ledger(
    question: str,
    session_ids: list[Any],
    sessions: list[list[dict[str, Any]]],
    dates: list[Any],
    ranked_ids: list[Any],
    *,
    budget_tokens: int = 12_000,
) -> tuple[str, list[str], dict[str, Any]]:
    """Build a bounded, deterministic extractive evidence ledger.

    The selector receives only the question and retrieved source material. It
    emits every valid retrieved source in a compact provenance index, then packs
    high-signal user statements and clearly marked assistant context. Repeated
    normalized statements share one entry with all source references; numeric and
    date tokens remain part of the identity key.
    """
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    if budget_tokens > _LEDGER_HARD_MAX_TOKENS:
        raise ValueError(f"budget_tokens must be <= {_LEDGER_HARD_MAX_TOKENS}")

    source_dates = list(dates or [])
    if len(source_dates) < len(session_ids):
        source_dates.extend([None] * (len(session_ids) - len(source_dates)))
    by_id = {
        sid: (turns if isinstance(turns, list) else [], source_dates[index])
        for index, (sid, turns) in enumerate(zip(session_ids, sessions))
    }
    unique_ranked: list[str] = []
    seen: set[str] = set()
    for sid in ranked_ids:
        if sid in by_id and sid not in seen:
            unique_ranked.append(sid)
            seen.add(sid)

    query_terms = _tokens(question)
    raw: list[dict[str, Any]] = []
    for rank, sid in enumerate(unique_ranked):
        turns, date = by_id[sid]
        local: list[dict[str, Any]] = []
        for turn_index, turn in enumerate(turns):
            if not isinstance(turn, dict):
                continue
            role = "user" if str(turn.get("role", "")).lower() == "user" else "assistant-context"
            for sentence_index, text in enumerate(_ledger_sentences(turn.get("content", ""))):
                score = _ledger_score(text, "user" if role == "user" else "assistant", query_terms)
                local.append({
                    "rank": rank,
                    "sid": sid,
                    "date": date,
                    "date_key": _ledger_date_key(date),
                    "turn": turn_index + 1,
                    "sentence": sentence_index,
                    "role": role,
                    "text": text,
                    "score": score,
                })
        users = sorted(
            (item for item in local if item["role"] == "user"),
            key=lambda item: (-float(item["score"]), int(item["turn"]), int(item["sentence"])),
        )[:_LEDGER_USER_LIMIT]
        assistants = sorted(
            (item for item in local if item["role"] == "assistant-context" and float(item["score"]) >= 8.0),
            key=lambda item: (-float(item["score"]), int(item["turn"]), int(item["sentence"])),
        )[:_LEDGER_ASSISTANT_LIMIT]
        raw.extend(users + assistants)

    grouped: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for item in raw:
        key = (str(item["role"]), _ledger_key(str(item["text"])))
        current = grouped.get(key)
        ref = {
            "rank": int(item["rank"]),
            "sid": str(item["sid"]),
            "date": item["date"],
            "turn": int(item["turn"]),
        }
        if current is None:
            grouped[key] = {**item, "refs": [ref]}
        else:
            current["refs"].append(ref)
            current["score"] = max(float(current["score"]), float(item["score"]))

    entries = list(grouped.values())
    for entry in entries:
        entry["refs"] = sorted(
            entry["refs"],
            key=lambda ref: (int(ref["rank"]), _ledger_date_key(ref["date"]), int(ref["turn"])),
        )
    entries.sort(
        key=lambda item: (
            _ledger_date_key(item["date"]),
            int(item["rank"]),
            int(item["turn"]),
            int(item["sentence"]),
            0 if item["role"] == "user" else 1,
        )
    )

    lines = [
        f"[Evidence ledger | deterministic extractive | budget={budget_tokens} tokens]",
        "[Sources]",
    ]
    for rank, sid in enumerate(unique_ranked, 1):
        _turns, date = by_id[sid]
        lines.append(
            f"[rank={rank} session={_ledger_safe_label(sid, 'unknown')} "
            f"date={_ledger_safe_label(date, 'unknown')} ]"
        )
    lines.append("[Statements]")
    emitted = 0
    skipped = 0
    for entry in entries:
        refs = ",".join(
            f"{ref['sid']}:turn={ref['turn']}" for ref in entry["refs"]
        )
        primary = entry["refs"][0]
        line = (
            f"- [{entry['role']}] session={primary['sid']} "
            f"date={_ledger_safe_label(primary['date'], 'unknown')} "
            f"turn={primary['turn']} sources={refs} :: {entry['text']}"
        )
        candidate = "\n".join(lines + [line])
        if _ledger_estimate(candidate) > budget_tokens:
            skipped += 1
            continue
        lines.append(line)
        emitted += 1

    context = "\n".join(lines)
    clipped = False
    if _ledger_estimate(context) > budget_tokens:
        context = context[: budget_tokens * 4]
        clipped = True
    telemetry = {
        "candidate_sessions": len(unique_ranked),
        "selected_sessions": len(unique_ranked),
        "source_rows": len(unique_ranked),
        "candidate_statements": len(raw),
        "deduplicated_items": len(raw) - len(entries),
        "emitted_statements": emitted,
        "skipped_statements": skipped,
        "estimated_tokens": _ledger_estimate(context),
        "budget_tokens": budget_tokens,
        "hard_max_tokens": _LEDGER_HARD_MAX_TOKENS,
        "clipped": clipped,
        "forbidden_fields_read": 0,
        "provider_calls": 0,
    }
    return context, unique_ranked, telemetry

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
    scored by question-token overlap and, for ``preference-structured``, direct
    user-evidence markers. The output contains at most ``max_windows_per_session``
    pairs per session and never exceeds ``budget_tokens`` under the same chars/4
    estimate used by the fused recall contract.

    Returns ``(context_text, selected_session_ids, telemetry)``. The selected
    IDs are unique and ordered by the first selected window's producer rank.
    Unknown/duplicate producer IDs are ignored so a malformed response cannot
    duplicate context silently.
    """
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    if max_windows_per_session <= 0:
        raise ValueError("max_windows_per_session must be positive")
    if guidance not in {"none", "preference", "preference-structured", "evidence-structured"}:
        raise ValueError(f"unknown context guidance: {guidance}")
    structured = guidance in {"preference-structured", "evidence-structured"}
    preference_structured = guidance == "preference-structured"
    if guidance == "preference-structured":
        guidance_text = _PREFERENCE_STRUCTURED_GUIDANCE
    elif guidance == "evidence-structured":
        guidance_text = _EVIDENCE_STRUCTURED_GUIDANCE
    elif guidance == "preference":
        guidance_text = _PREFERENCE_GUIDANCE
    else:
        guidance_text = ""
    guidance_cost = max(1, len(guidance_text) // 4) if guidance_text else 0
    selection_budget = max(0, budget_tokens - guidance_cost)

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
    structured_evidence: list[dict[str, Any]] = []
    for rank, sid in enumerate(unique_ranked):
        turns, date = by_id[sid]
        local: list[dict[str, Any]] = []
        # LongMemEval sessions alternate user/assistant turns. Pairing adjacent
        # turns preserves the question/answer unit without overlapping copies.
        for start in range(0, len(turns), 2):
            end = min(len(turns), start + 2)
            pair_turns = turns[start:end]
            body_renderer = _structured_turn_text if structured else _turn_text
            body = "\n".join(body_renderer(turn) for turn in pair_turns)
            local.append(
                {
                    "rank": rank,
                    "sid": sid,
                    "date": date,
                    "start": start,
                    "end": end,
                    "body": body,
                    "overlap": len(query_terms & _tokens(body)),
                    "preference_score": _preference_score(pair_turns, query_terms),
                    "fact_score": _fact_score(pair_turns, query_terms),
                    "cost": max(1, len(body) // 4),
                }
            )

        if structured and local:
            score_key = "preference_score" if preference_structured else "fact_score"
            evidence = max(
                local,
                key=lambda item: (
                    float(item[score_key]),
                    int(item["overlap"]),
                    -int(item["start"]),
                ),
            )
            user_lines = [
                f"[User-stated evidence]\nuser: {turn.get('content', '')}"
                for turn in turns[evidence["start"]:evidence["end"]]
                if isinstance(turn, dict)
                and str(turn.get("role", "")).lower() == "user"
            ]
            if user_lines and float(evidence[score_key]) > 0:
                structured_evidence.append({
                    "rank": rank,
                    "sid": sid,
                    "date": date,
                    "start": evidence["start"],
                    "end": evidence["end"],
                    "body": "\n".join(user_lines),
                    "score": evidence[score_key],
                })
        local.sort(key=lambda item: (-int(item["overlap"]), int(item["start"])))
        local = local[:max_windows_per_session]
        candidates.extend(local)

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

    evidence_used = used
    evidence_appended = 0
    evidence_represented = 0
    evidence_header = (
        "[Retrieved user-stated preference evidence]"
        if preference_structured
        else "[Retrieved user-stated fact evidence]"
    )
    evidence_header_cost = max(1, len(evidence_header) // 4)
    selected_keys = {(item["sid"], item["start"], item["end"]) for item in selected}
    evidence_blocks: list[str] = []
    if structured:
        for item in structured_evidence:
            key = (item["sid"], item["start"], item["end"])
            if key in selected_keys:
                evidence_represented += 1
                continue
            session_header = _date_header(
                item["sid"], item["date"], int(item["start"]), int(item["end"])
            )[1:-1]
            label = "Preference evidence" if preference_structured else "User-stated evidence"
            block = (
                f"[{label} | {session_header}]\n"
                f"{item['body']}"
            )
            cost = max(1, len(block) // 4)
            if not evidence_blocks:
                cost += evidence_header_cost
            if evidence_used + cost > selection_budget:
                continue
            evidence_blocks.append(block)
            evidence_used += cost
            evidence_appended += 1
            evidence_represented += 1

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

    if evidence_blocks:
        blocks.append(evidence_header + "\n" + "\n\n".join(evidence_blocks))
    context_body = "\n\n".join(blocks) or "(no prior conversation history is available)"
    context = f"{guidance_text}\n\n{context_body}" if guidance_text else context_body
    telemetry = {
        "candidate_sessions": len(unique_ranked),
        "candidate_windows": len(candidates),
        "selected_windows": len(selected),
        "selected_sessions": len(selected_ids),
        "evidence_windows": evidence_represented,
        "evidence_appended": evidence_appended,
        "preference_evidence_windows": evidence_represented if preference_structured else 0,
        "preference_evidence_appended": evidence_appended if preference_structured else 0,
        "estimated_tokens": min(evidence_used + guidance_cost, budget_tokens),
        "skipped_windows": skipped,
        "clipped_windows": clipped,
        "max_windows_per_session": max_windows_per_session,
        "budget_tokens": budget_tokens,
        "guidance_applied": guidance,
    }
    return context, selected_ids, telemetry
