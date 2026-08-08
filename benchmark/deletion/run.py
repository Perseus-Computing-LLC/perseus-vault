#!/usr/bin/env python3
"""Deletion durability benchmark for logical forget and permanent purge."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import shutil
import queue
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
from benchmark.package.common.publication import build_common_report


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def find_binary(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("PERSEUS_VAULT_BIN"), os.environ.get("MIMIR_BIN")]
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        candidates.extend((str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("perseus-vault binary not found; build it or pass --bin")


class Client:
    def __init__(self, binary: str, db: Path):
        self.p = subprocess.Popen([binary, "--db", str(db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.responses: queue.Queue[object] = queue.Queue()
        threading.Thread(target=self._reader_loop, daemon=True).start()
        self.i = 0
        self.send({"jsonrpc": "2.0", "id": self.next_id(), "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "deletion-benchmark", "version": "1"}}})
        self.read()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def next_id(self) -> int:
        self.i += 1
        return self.i

    def _reader_loop(self) -> None:
        assert self.p.stdout is not None
        for line in self.p.stdout:
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "result" in response or "error" in response:
                self.responses.put(response)
        self.responses.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.p.stdin.flush()

    def read(self) -> dict[str, Any]:
        assert self.p.stdout is not None
        response = self.responses.get(timeout=30)
        if response is None or not isinstance(response, dict):
            raise RuntimeError("Vault closed MCP stream")
        if "error" in response:
            raise RuntimeError("MCP request failed")
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError("MCP tool returned an error")
        if isinstance(result, dict) and result.get("content"):
            text = result["content"][0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
        return result

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.send({"jsonrpc": "2.0", "id": self.next_id(), "method": "tools/call", "params": {"name": name, "arguments": args}})
        return self.read()

    def close(self) -> None:
        try:
            if self.p.stdin is not None:
                self.p.stdin.close()
            self.p.wait(timeout=30)
        except Exception:
            self.p.kill()
            self.p.wait(timeout=5)


def remember(c: Client, category: str, key: str, marker: str) -> None:
    c.call("perseus_vault_remember", {"category": category, "key": key, "body_json": stable_json({"marker": marker}), "skip_dedup": True})


def recall(c: Client, category: str, query: str, include_archived: bool = False) -> dict[str, Any]:
    # FTS5 tokenizes markers; use a distinctive two-token query rather than a
    # full hyphenated marker, which can otherwise become a no-match probe.
    probe = query.replace("-", " ")
    return c.call("perseus_vault_recall", {"query": probe, "category": category, "mode": "fts5", "limit": 100, "min_decay": 0, "trust_weight": 0, "include_archived": include_archived, "include_outcome": True})


def direct_items(c: Client, category: str, include_archived: bool = False) -> list[dict[str, Any]]:
    result = c.call("perseus_vault_recall", {"query": "", "category": category, "mode": "fts5", "limit": 100, "min_decay": 0, "trust_weight": 0, "include_archived": include_archived, "include_outcome": True})
    return result.get("items", []) if isinstance(result, dict) else []


def contains_marker(c: Client, category: str, marker: str, include_archived: bool = False) -> bool:
    return marker in stable_json(direct_items(c, category, include_archived))


def source_contains_marker(path: Path, marker: str) -> bool:
    return path.exists() and marker in path.read_text(errors="replace")


def make_report_rows(rows: list[dict[str, Any]]) -> str:
    return digest(stable_json(sorted(({"case": row["case"], "axis": row["axis"], "ok": bool(row["ok"])} for row in rows), key=lambda row: (row["case"], row["axis"]))))


def run_case(c: Client, case: dict[str, Any], db: Path, rows: list[dict[str, Any]]) -> None:
    category, key, canary, twin = case["category"], case["key"], case["canary"], case["twin"]
    remember(c, category, key, canary)
    remember(c, category, f"{key}-twin", twin)
    rows.append({"case": case["id"], "axis": "before_delete_readable", "ok": contains_marker(c, category, canary)})
    rows.append({"case": case["id"], "axis": "before_delete_twin_readable", "ok": contains_marker(c, category, twin)})
    c.call("perseus_vault_forget", {"category": category, "key": key, "reason": "benchmark deletion canary"})
    rows.append({"case": case["id"], "axis": "after_forget_hidden", "ok": not contains_marker(c, category, canary)})
    rows.append({"case": case["id"], "axis": "twin_survives_forget", "ok": contains_marker(c, category, twin)})
    c.call("perseus_vault_reindex", {})
    c.call("perseus_vault_cohere", {"dry_run": False, "archive_threshold": 0.0})
    rows.append({"case": case["id"], "axis": "after_background_hidden", "ok": not contains_marker(c, category, canary)})
    rows.append({"case": case["id"], "axis": "after_background_twin_survives", "ok": contains_marker(c, category, twin)})
    source = db.parent / f"{case['id']}.md"
    source.write_text(f"---\ncategory: {category}\nkey: {key}-reentry\n---\n\n{canary}\n")
    c.call("perseus_vault_markdown_import", {"path": str(source), "source_system": "deletion-benchmark"})
    c.call("perseus_vault_reindex", {})
    rows.append({"case": case["id"], "axis": "after_reingest_stays_hidden", "ok": not contains_marker(c, category, canary)})
    exported = db.parent / f"{case['id']}-derived.md"
    c.call("perseus_vault_derived_export", {"output_path": str(exported), "workspace_hash": ""})
    rows.append({"case": case["id"], "axis": "derived_export_excludes_canary", "ok": exported.exists() and not source_contains_marker(exported, canary)})
    if case["policy"] == "permanent_purge":
        c.call("perseus_vault_purge", {"dry_run": False})
        rows.append({"case": case["id"], "axis": "purge_removes_archived_canary", "ok": not contains_marker(c, category, canary, True)})
    else:
        rows.append({"case": case["id"], "axis": "logical_policy_preserves_twin", "ok": contains_marker(c, category, twin, True)})



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=None)
    ap.add_argument("--manifest", default=str(HERE / "manifest.json"))
    ap.add_argument("--out", default=str(HERE / "report.json"))
    args = ap.parse_args()
    binary = find_binary(args.bin)
    manifest = json.loads(Path(args.manifest).read_text())
    if not isinstance(manifest.get("cases"), list) or not manifest["cases"]:
        raise ValueError("deletion manifest must contain at least one case")
    db = Path(tempfile.mkdtemp(prefix="perseus-vault-deletion-")) / "deletion.db"
    c = None
    rows: list[dict[str, Any]] = []
    try:
        c = Client(binary, db)
        for case in manifest["cases"]:
            run_case(c, case, db, rows)
    finally:
        if c is not None:
            c.close()
        shutil.rmtree(db.parent, ignore_errors=True)
    passed = sum(bool(row["ok"]) for row in rows)
    if not rows:
        raise ValueError("deletion benchmark produced no checks")
    raw_report = {
        "passed": passed == len(rows),
        "status": "passed" if passed == len(rows) else "failed",
        "capabilities": {"runner": {"status": "available"}},
        "network_calls": 0,
        "cases": [
            {
                "id": f"{row['case']}-{row['axis']}".lower(),
                "category": "deletion",
                "status": "passed" if row["ok"] else "failed",
                "checks": {row["axis"]: bool(row["ok"])},
                "evidence": {"complete": True},
                **({"failure_class": "deletion_check_failed"} if not row["ok"] else {}),
            }
            for row in rows
        ],
        "metrics": {"deletion_durability": {"status": "available", "numerator": passed, "denominator": len(rows), "rate": passed / len(rows)}},
    }
    report = build_common_report(
        suite_id="perseus-vault-deletion-durability",
        suite_version="v1",
        raw_report=raw_report,
        binary=binary,
        manifest=manifest,
        profile={"suite": "deletion", "version": "v1", "network_calls": 0},
        repo_root=REPO,
        not_measured=[],
        claim_ids=["deletion-v1-local-durability"],
        negative_claim_ids=["provider-failure-stress", "deletion-external-propagation"],
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"checks_passed": passed, "checks_total": len(rows), "accuracy": passed / len(rows), "out": args.out}, sort_keys=True))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
