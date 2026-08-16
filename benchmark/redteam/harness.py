"""Perseus Vault memory red-team harness (MAFIA / MemCollusion / Chronos).

Skeleton phase (spec docs/specs/memory-red-team-harness.md): dataset schemas,
deterministic validators, and run-manifest machinery. Attack drivers and the
defense-eval layer land in follow-up phases — see the spec's implementation
slice.

All functions here are deterministic: no network, no time, no randomness.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ── dataset surfaces ──────────────────────────────────────────────────────

MAFIA_PROBE_SET = HERE / "datasets" / "mafia_probe_set.json"
SALAMI_SCENARIOS = HERE / "datasets" / "salami_scenarios.json"
BENIGN_POOLS = HERE / "datasets" / "benign_pools.json"

IMPERATIVE_CUES = re.compile(
    r"\b(you must|must (?:always|never)|ignore (?:all )?(?:prior |previous )?"
    r"instructions|disregard|override|forget all|you are now|"
    r"system prompt|as an ai|pretend|role ?play)\b",
    re.IGNORECASE,
)


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text())


# ── MAFIA: probe-set validation ───────────────────────────────────────────

def validate_probe_set(data: object) -> list[str]:
    """Structural checks for the MAFIA probe set (worked dataset)."""
    errors: list[str] = []
    if not isinstance(data, dict) or "probes" not in data:
        return ["probe set must be an object with a 'probes' list"]
    probes = data["probes"]
    if not isinstance(probes, list) or not probes:
        errors.append("'probes' must be a non-empty list")
        return errors
    seen: set[str] = set()
    for p in probes:
        pid = p.get("id", "")
        if not pid:
            errors.append("probe missing 'id'")
        elif pid in seen:
            errors.append(f"duplicate probe id {pid}")
        seen.add(pid)
        for field in ("seed_query", "schema", "target_pair"):
            if not p.get(field):
                errors.append(f"probe {pid or '?'} missing '{field}'")
        if not p.get("surfaced_questions"):
            errors.append(f"probe {pid} missing 'surfaced_questions'")
        if "allocated_cluster" not in p or not isinstance(p.get("allocated_cluster"), int):
            errors.append(f"probe {pid} missing int 'allocated_cluster'")
    return errors


def cloak_lint(payload: str) -> list[str]:
    """Compact factual cloaks carry no imperative or instruction-like cues.

    Deterministic surface-form proxy for MAFIA §4.3; the semantic-similarity
    half of cloak quality is LLM-judged in phase 3 (not_measured here).
    """
    hits = IMPERATIVE_CUES.findall(payload or "")
    return [f"imperative cue '{h}'" for h in hits]


# ── MemCollusion: four construction constraints ───────────────────────────

def collusion_anchor_coverage(scenario: dict) -> list[str]:
    """Constraint 1: the coalition covers every goal anchor."""
    goal_anchors = {a.lower() for a in scenario.get("goal_anchors", [])}
    union: set[str] = set()
    for f in scenario.get("fragments", []):
        union.update(a.lower() for a in f.get("anchors", []))
    missing = sorted(goal_anchors - union)
    return [f"anchor uncovered: {m}" for m in missing]


def collusion_single_fragment_innocence(scenario: dict) -> list[str]:
    """Constraint 2 (deterministic proxy): no single fragment carries the full
    goal-anchor set, and no fragment matches the victim query verbatim."""
    goal_anchors = {a.lower() for a in scenario.get("goal_anchors", [])}
    victim = (scenario.get("victim_query") or "").strip().lower()
    problems: list[str] = []
    for f in scenario.get("fragments", []):
        anchors = {a.lower() for a in f.get("anchors", [])}
        if goal_anchors and goal_anchors <= anchors:
            problems.append(f"fragment {f.get('id')} covers the full goal")
        surface = (f.get("surface_form") or "").strip().lower()
        if victim and surface == victim:
            problems.append(f"fragment {f.get('id')} equals the victim query")
    return problems


def collusion_naturalness(scenario: dict) -> list[str]:
    """Constraint 3 (surface-form proxy): fragments must read as naturally
    distillable platform content — no imperative cues, no empty surfaces."""
    problems: list[str] = []
    for f in scenario.get("fragments", []):
        if not (f.get("surface_form") or "").strip():
            problems.append(f"fragment {f.get('id')} has empty surface_form")
            continue
        problems.extend(
            f"fragment {f.get('id')} carries {cue}"
            for cue in cloak_lint(f["surface_form"])
        )
    return problems


def collusion_mutual_consistency(scenario: dict) -> list[str]:
    """Constraint 4 (declared-slot proxy): fragments asserting opposite values
    for the same declared slot are inconsistent."""
    slots: dict[str, str] = {}
    problems: list[str] = []
    for f in scenario.get("fragments", []):
        for slot in f.get("declared_slots", []):
            key = slot["slot"].lower()
            value = str(slot["value"]).lower()
            if key in slots and slots[key] != value:
                problems.append(
                    f"fragment {f.get('id')} contradicts {key}={slots[key]!r} "
                    f"(prior {value!r})"
                )
            slots[key] = value
    return problems


def validate_salami_scenarios(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict) or "scenarios" not in data:
        return ["scenario file must be an object with a 'scenarios' list"]
    seen: set[str] = set()
    for s in data["scenarios"]:
        sid = s.get("id", "")
        if not sid:
            errors.append("scenario missing 'id'")
        elif sid in seen:
            errors.append(f"duplicate scenario id {sid}")
        seen.add(sid)
        if s.get("category") not in {
            "preference_manipulation", "web_shopping", "privacy_extraction",
        }:
            errors.append(f"scenario {sid} has unknown category {s.get('category')!r}")
        if not s.get("goal_anchors"):
            errors.append(f"scenario {sid} missing 'goal_anchors'")
        if not s.get("victim_query"):
            errors.append(f"scenario {sid} missing 'victim_query'")
        errors.extend(collusion_anchor_coverage(s))
        errors.extend(collusion_single_fragment_innocence(s))
        errors.extend(collusion_naturalness(s))
        errors.extend(collusion_mutual_consistency(s))
    return errors


# ── run manifest + report signing ─────────────────────────────────────────

def validate_run_manifest(data: dict) -> list[str]:
    errors: list[str] = []
    for field in ("harness_sha256", "dataset_sha256", "binary_commit"):
        if not data.get(field):
            errors.append(f"run manifest missing '{field}'")
    if not isinstance(data.get("seed"), int):
        errors.append("run manifest seed must be an int (0 is valid)")
    judge = data.get("judge") or {}
    for field in ("model", "prompt_sha256"):
        if not judge.get(field):
            errors.append(f"run manifest judge missing '{field}'")
    if not isinstance(judge.get("temperature", 0.0), (int, float)):
        errors.append("judge.temperature must be numeric")
    budgets = data.get("budgets") or {}
    if not isinstance(budgets.get("probes"), int) or not isinstance(budgets.get("poison_writes"), int):
        errors.append("budgets.probes / budgets.poison_writes must be ints")
    return errors


def manifest_sha256() -> str:
    """Content hash of the harness sources (run + validators) — pinned into
    every run manifest so reports are reproducible."""
    h = hashlib.sha256()
    for name in ("harness.py", "run.py", "test_harness.py"):
        h.update(name.encode())
        h.update(Path(HERE, name).read_bytes())
    return h.hexdigest()


def dataset_sha256() -> str:
    h = hashlib.sha256()
    for name in ("mafia_probe_set.json", "salami_scenarios.json", "benign_pools.json"):
        h.update(Path(HERE, "datasets", name).read_bytes())
    return h.hexdigest()


def sign_report(report: dict, manifest: dict) -> str:
    """Deterministic report signing stub: sha256 over canonical JSON of
    (report, manifest). Phase 3 replaces the plain digest with an
    audit-chain receipt (authority-trace suite format)."""
    payload = json.dumps(
        {"report": report, "manifest": manifest},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()
