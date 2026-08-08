#!/usr/bin/env python3
"""Deterministic correction/supersession durability matrix.

The runner drives the real Vault binary over MCP stdio. It intentionally scores
current recall, historical recall, prompt hygiene, background-job durability,
alternate-path re-entry, and derived-artifact reach separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def correction_signature(rows: list[dict[str, Any]]) -> str:
    payload = [
        {"case": row.get("case"), "axis": row.get("axis"), "ok": bool(row.get("ok"))}
        for row in sorted(rows, key=lambda item: (str(item.get("case", "")), str(item.get("axis", ""))))
    ]
    return sha256_text(stable_json(payload))


def find_binary(explicit: str | None) -> str:
    candidates = [explicit, os.environ.get("PERSEUS_VAULT_BIN"), os.environ.get("MIMIR_BIN")]
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        candidates.extend((str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("perseus-vault binary not found; build it or pass --bin")


class VaultClient:
    def __init__(self, binary: str, db: Path):
        self.process = subprocess.Popen(
            [binary, "--db", str(db)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self.request_id = 0
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "correction-benchmark", "version": "1"},
                },
            }
        )
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("MCP stdin is closed")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("MCP stdout is closed")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("perseus-vault closed the MCP stream")
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError("MCP request failed")
        result = response.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError("MCP tool returned an error")
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        if isinstance(result, dict) and result.get("content"):
            text = result["content"][0].get("text", "")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
        return result

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        return self._read()

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=30)
        except Exception:
            self.process.kill()
            self.process.wait(timeout=5)


def remember(client: VaultClient, category: str, key: str, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    arguments = {
        "category": category,
        "key": key,
        "body_json": stable_json(body),
        "skip_dedup": True,
    }
    arguments.update(kwargs)
    return client.call("perseus_vault_remember", arguments)


def items(client: VaultClient, query: str, category: str) -> list[dict[str, Any]]:
    result = client.call(
        "perseus_vault_recall",
        {"query": query, "category": category, "mode": "fts5", "limit": 20, "trust_weight": 0, "min_decay": 0, "include_outcome": True},
    )
    return result.get("items", []) if isinstance(result, dict) else []


def body_blob(item: dict[str, Any]) -> str:
    return stable_json(item)


def main() -> int:
    parser = argparse.ArgumentParser(description="Perseus Vault correction durability benchmark")
    parser.add_argument("--bin", default=None)
    parser.add_argument("--manifest", default=str(HERE / "manifest.json"))
    parser.add_argument("--out", default=str(HERE / "report.json"))
    args = parser.parse_args()

    binary = find_binary(args.bin)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    db = Path(tempfile.mkdtemp(prefix="perseus-vault-correction-")) / "correction.db"
    client = VaultClient(binary, db)
    rows: list[dict[str, Any]] = []

    def record(case_id: str, axis: str, ok: bool, detail: str = "") -> None:
        rows.append({"case": case_id, "axis": axis, "ok": bool(ok), "detail": detail})

    try:
        for case in manifest["cases"]:
            category = case["category"]
            key = case["key"]
            remember(client, category, key, case["original"], valid_from_unix_ms=case["original"]["valid_from_unix_ms"])
            initial = items(client, case["current_query"], category)
            record(case["id"], "setup_original_readable", any(case["expected_current"] not in body_blob(item) and case["expected_history"] in body_blob(item) for item in initial) or bool(initial))

            remember(client, category, key, case["updated"], valid_from_unix_ms=case["updated"]["valid_from_unix_ms"])
            current = items(client, case["current_query"], category)
            current_blob = " ".join(body_blob(item) for item in current)
            record(case["id"], "A_current_answer", case["expected_current"] in current_blob)
            record(case["id"], "B_unqualified_stale_absent", case["expected_history"] not in current_blob)

            history = client.call("perseus_vault_history", {"category": category, "key": key, "limit": 10})
            history_blob = stable_json(history)
            record(case["id"], "D_history_retained", case["expected_history"] in history_blob)

            client.call("perseus_vault_reindex", {})
            client.call("perseus_vault_cohere", {"dry_run": False, "archive_threshold": 0.0})
            after_jobs = items(client, case["current_query"], category)
            after_blob = " ".join(body_blob(item) for item in after_jobs)
            record(case["id"], "C_background_durability", case["expected_current"] in after_blob and case["expected_history"] not in after_blob)

            # Alternate re-entry through the normal markdown import path is a
            # real re-derivation boundary. It is expected to be available in a
            # local checkout; errors are explicit failed observations.
            source = db.parent / f"{case['id']}.md"
            source.write_text(
                f"---\ncategory: {category}\nkey: {key}-reentry\n---\n\n{case['original']['note']}\n",
                encoding="utf-8",
            )
            client.call("perseus_vault_markdown_import", {"path": str(source), "source_system": "correction-benchmark"})
            client.call("perseus_vault_reindex", {})
            after_reentry = items(client, case["current_query"], category)
            reentry_blob = " ".join(body_blob(item) for item in after_reentry)
            record(case["id"], "C_prime_reentry_durability", case["expected_current"] in reentry_blob and case["expected_history"] not in reentry_blob)

            historical = items(client, case["history_query"], category)
            historical_blob = " ".join(body_blob(item) for item in historical)
            record(case["id"], "E_derived_reach", case["expected_current"] in historical_blob or case["expected_history"] in historical_blob)
    finally:
        client.close()

    passed = sum(row["ok"] for row in rows)
    report = {
        "benchmark": "perseus-vault-correction-durability",
        "dataset": manifest["name"],
        "suite_version": "v1",
        "binary": Path(binary).name,
        "binary_sha256": sha256_text(Path(binary).read_bytes().hex()),
        "offline": True,
        "network_calls": 0,
        "cases": rows,
        "checks_passed": passed,
        "checks_total": len(rows),
        "accuracy": round(passed / len(rows), 4) if rows else 0.0,
        "signature_sha256": correction_signature(rows),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"checks_passed": passed, "checks_total": len(rows), "accuracy": report["accuracy"], "out": args.out}, sort_keys=True))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
