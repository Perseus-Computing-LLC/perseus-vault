"""Attribution-ladder benchmark (#1049).

Measures how answer quality / refusal rate changes with *attribution
presentation* in the rendered recall payload — same store, same queries, same
grader; only the render shape varies (the Coalent-style ladder:
bare text -> key -> key|source -> key|source|date).

The deterministic judge ("resolver") simulates an answerer that can only use
the fields a shape actually renders. Per-outlet / per-outlet-as-of questions
therefore REFUSE under shapes that hide source/date, reproducing the measured
Coalent mechanism (0.641 -> 0.678 -> 0.731) with a fixed, offline grader.

An optional LLM judge (`--judge llm`) re-grades the same renders with a
bounded OpenAI chat-completions pass (gpt-4o-mini, spend-gated by --limit).

Design contract: this harness never calls a model by default; retrieval and
rendering stay separable (each query records `retrieval_ok` independently of
the render verdict).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

SCHEMA_VERSION = "perseus-vault-attribution-ladder/v1"
SHAPES = ("bare", "key_only", "key_source", "key_source_time")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def find_binary(explicit: "str | None") -> str:
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("PERSEUS_VAULT_BIN"):
        candidates.append(os.environ["PERSEUS_VAULT_BIN"])
    exe = "perseus-vault.exe" if os.name == "nt" else "perseus-vault"
    candidates += [
        str(REPO / "target" / "release" / exe),
        str(REPO / "target" / "debug" / exe),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(Path(c).resolve())
    sys.exit(
        "error: perseus-vault binary not found. Build it (`cargo build --release`) "
        "or pass --bin / set PERSEUS_VAULT_BIN."
    )


class PerseusVault:
    """One MCP tools/call per process (matches benchmark/recall/run.py)."""

    def __init__(self, binary: str, db: str, env: "dict | None" = None):
        self.binary = binary
        self.db = db
        self.env = env

    def call(self, name: str, args: dict):
        p = subprocess.Popen(
            [self.binary, "--db", self.db],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=self.env,
        )
        w = p.stdin.write
        w(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "attribution-ladder-bench", "version": "1"},
                    },
                }
            )
            + "\n"
        )
        p.stdin.flush()
        p.stdout.readline()
        w(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        w(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                }
            )
            + "\n"
        )
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


def normalize_item(item: dict) -> dict:
    """Normalize a recall item to {key, text, source, date} with None where
    the store did not provide a field (the renderer then hides it)."""
    body = item.get("body_json")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                item = {**item, **parsed}
        except Exception:
            pass
    elif isinstance(body, dict):
        item = {**item, **body}
    text = item.get("text") or item.get("note") or item.get("content") or item.get("summary") or ""
    return {
        "key": item.get("key") or item.get("id") or "?",
        "text": str(text),
        "source": item.get("source"),
        "date": item.get("valid_from") or item.get("date"),
    }


def render_item(item: dict, shape: str) -> str:
    """Render one payload item in the given shape. Fields the shape does not
    carry are absent from the string AND from the visible-field mask."""
    text = item.get("text", "")
    if shape == "bare":
        return text
    key = item.get("key", "?")
    if shape == "key_only":
        return f"[{key}] {text}"
    if shape == "key_source":
        src = item.get("source") if item.get("source") is not None else "?"
        return f"[{key} | {src}] {text}"
    if shape == "key_source_time":
        src = item.get("source") if item.get("source") is not None else "?"
        date = item.get("date") if item.get("date") is not None else "?"
        return f"[{key} | {src} | {date}] {text}"
    raise ValueError(f"unknown shape {shape}")


def visible_fields(item: dict, shape: str) -> dict:
    """The fields an answerer can rely on after reading this shape's render."""
    v = {"text": item.get("text", "")}
    if shape in ("key_only", "key_source", "key_source_time"):
        v["key"] = item.get("key")
    if shape in ("key_source", "key_source_time"):
        v["source"] = item.get("source")
    if shape == "key_source_time":
        v["date"] = item.get("date")
    return v


def resolver_verdict(query: dict, items: list[dict], shape: str) -> str:
    """Deterministic, shape-aware grader.

    Returns 'correct' | 'partial' | 'refusal'. The resolver can only use the
    visible fields of the shape (the simulated answerer reads the render).
    """
    facts = query.get("facts", [])
    kind = query.get("kind", "plain")
    constraints = query.get("resolve", {})

    # An absent query can never be answered from any payload.
    if kind == "absent":
        return "refusal"

    best = None
    for item in items:
        vis = visible_fields(item, shape)
        if constraints.get("source") is not None and vis.get("source") != constraints["source"]:
            continue
        if constraints.get("key") is not None and vis.get("key") != constraints["key"]:
            continue
        if kind == "outlet_asof" and vis.get("date") is None:
            # The question pins a date; an answerer with no date field cannot
            # verify currency -> cannot answer (honest refusal).
            continue
        if constraints.get("as_of") is not None and vis.get("date") is not None:
            if vis["date"] > constraints["as_of"]:
                continue
        if kind == "plain" and not vis.get("text"):
            continue
        score = sum(1 for f in facts if f in (vis.get("text") or ""))
        if best is None or score > best[0]:
            best = (score, len(facts), item["key"])
    if best is None:
        return "refusal"
    if best[0] >= len(facts) and len(facts) > 0:
        return "correct"
    if best[0] > 0:
        return "partial"
    return "refusal"


def render_payload(items: list[dict], shape: str) -> str:
    return "\n".join(render_item(it, shape) for it in items)


def llm_judge(query: dict, payload: str, api_key: str, model: str) -> str:
    """Bounded LLM re-grade: answer or REFUSAL. Returns correct/partial/refusal."""
    import urllib.request

    prompt = (
        "You are a strict memory answerer. Answer the question using ONLY the "
        "retrieved payload below. If the payload does not contain enough "
        "information to answer (missing source, missing date, or the fact "
        "itself), reply exactly REFUSAL.\n\n"
        f"Question: {query['q']}\n\nPayload:\n{payload}\n\n"
        "Reply with the answer only, or REFUSAL."
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 60,
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read().decode())
    answer = resp["choices"][0]["message"]["content"].strip()
    if answer.strip().upper() == "REFUSAL":
        return "refusal"
    facts = query.get("facts", [])
    if all(f.lower() in answer.lower() for f in facts):
        return "correct"
    if any(f.lower() in answer.lower() for f in facts):
        return "partial"
    return "refusal"


def verify_report(report: dict, rows: list[dict]) -> bool:
    """Structural self-check: digest matches, per-shape aggregates recompute."""
    if report["signature"]["value"] != sha256_json(report["signature"]["inputs"]):
        return False
    for shape in SHAPES:
        agg = report["shapes"][shape]
        n = len([r for r in rows if r["shape"] == shape])
        if agg["n"] != n:
            return False
        correct = sum(1 for r in rows if r["shape"] == shape and r["verdict"] == "correct")
        refusal = sum(1 for r in rows if r["shape"] == shape and r["verdict"] == "refusal")
        partial = sum(1 for r in rows if r["shape"] == shape and r["verdict"] == "partial")
        if agg["correct"] != correct or agg["refusal"] != refusal or agg["partial"] != partial:
            return False
    return True


def run_benchmark(dataset: dict, judge: str = "deterministic", limit: "int | None" = None,
                  api_key: "str | None" = None, model: str = "gpt-4o-mini") -> tuple[dict, list[dict]]:
    binary = find_binary(None)
    db_dir = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    db = str(db_dir / "perseus_vault-attribution-ladder.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass

    m = PerseusVault(binary, db)

    # 1. Ingest the fixed store.
    for mem in dataset["store"]:
        body = {"text": mem["text"]}
        if mem.get("source"):
            body["source"] = mem["source"]
        if mem.get("valid_from"):
            body["valid_from"] = mem["valid_from"]
        m.call(
            "perseus_vault_remember",
            {
                "category": mem.get("category", "insight"),
                "key": mem["key"],
                "body_json": json.dumps(body),
                "type": mem.get("type", "fact"),
            },
        )

    # 2. Recall per query, render per shape, grade.
    queries = dataset["queries"]
    if limit:
        queries = queries[:limit]
    rows = []
    for q in queries:
        # #1049: grade the FULL match set (not the top-N): the fts5 arm orders
        # by retrieval_count DESC (accumulated across the sweep's own queries),
        # so a top-N payload would collapse to the same count-dominant set and
        # confound the ladder. The ladder isolates RENDERING — retrieval_ok
        # records that the target was recalled at all (the Coalent claim: the
        # accuracy swing is driven by how provenance is rendered, not by
        # retrieval quality).
        r = m.call(
            "perseus_vault_recall",
            {"query": q["q"], "mode": "fts5", "limit": q.get("limit", 100)},
        )
        items = [normalize_item(it) for it in (r.get("items", []) if isinstance(r, dict) else [])]
        expected_keys = set(q.get("resolve", {}).get("key", "").split(",")) if q.get("resolve", {}).get("key") else set()
        retrieval_ok = any(
            it["key"] in expected_keys or (q.get("resolve", {}).get("source") and it["source"] == q["resolve"]["source"])
            for it in items
        ) if (expected_keys or q.get("resolve", {}).get("source")) else True
        for shape in SHAPES:
            verdict = resolver_verdict(q, items, shape)
            if judge == "llm" and api_key:
                verdict = llm_judge(q, render_payload(items, shape), api_key, model)
            rows.append(
                {
                    "query": q["q"],
                    "kind": q.get("kind", "plain"),
                    "shape": shape,
                    "verdict": verdict,
                    "retrieval_ok": bool(retrieval_ok),
                    "payload_keys": [it["key"] for it in items[:10]],
                }
            )

    shapes = {shape: {"n": 0, "correct": 0, "refusal": 0, "partial": 0} for shape in SHAPES}
    for row in rows:
        agg = shapes[row["shape"]]
        agg["n"] += 1
        agg[row["verdict"]] += 1
    for agg in shapes.values():
        agg["accuracy"] = round(agg["correct"] / agg["n"], 4) if agg["n"] else 0.0
        agg["refusal_rate"] = round(agg["refusal"] / agg["n"], 4) if agg["n"] else 0.0

    inputs = {
        "schema": SCHEMA_VERSION,
        "dataset_digest": sha256_json(dataset),
        "judge": judge,
        "n_queries": len(queries),
        "shapes": list(SHAPES),
        "binary": binary,
    }
    report = {
        "schema": SCHEMA_VERSION,
        "inputs": inputs,
        "shapes": shapes,
        "retrieval_ok_rate": round(
            sum(1 for r in rows if r["retrieval_ok"]) / len(rows), 4
        ) if rows else 0.0,
        "signature": {"value": sha256_json(inputs), "inputs": inputs},
    }
    return report, rows


def write_outputs(out_dir: Path, report: dict, rows: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out_dir / "rows.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows), encoding="utf-8"
    )
    (out_dir / "ladder.md").write_text(ladder_markdown(report), encoding="utf-8")


def ladder_markdown(report: dict) -> str:
    lines = ["| Shape | n | correct | refusal | partial | accuracy | refusal_rate |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for shape in SHAPES:
        a = report["shapes"][shape]
        lines.append(
            f"| {shape} | {a['n']} | {a['correct']} | {a['refusal']} | {a['partial']} "
            f"| {a['accuracy']:.3f} | {a['refusal_rate']:.3f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Attribution-ladder benchmark (#1049)")
    ap.add_argument("--bin", default=None, help="Path to the perseus-vault binary")
    ap.add_argument("--dataset", default=str(HERE / "dataset.json"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--judge", choices=("deterministic", "llm"), default="deterministic")
    ap.add_argument("--limit", type=int, default=None, help="Cap the number of queries (LLM spend gate)")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    api_key = os.environ.get("OPENAI_API_KEY")
    if args.judge == "llm" and not api_key:
        sys.exit("--judge llm requires OPENAI_API_KEY in the environment")
    report, rows = run_benchmark(
        dataset, judge=args.judge, limit=args.limit, api_key=api_key, model=args.model
    )
    if not verify_report(report, rows):
        raise SystemExit("internal report verification failed")
    out = Path(args.out)
    write_outputs(out, report, rows)
    print(ladder_markdown(report))
    print(json.dumps({"output": str(out), "rows": len(rows), "report_digest": report["signature"]["value"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
