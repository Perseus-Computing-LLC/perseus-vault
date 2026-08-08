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
import shutil
import queue
import signal
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


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
            start_new_session=(os.name != "nt"),
        )
        try:
            self._reader = threading.Thread(target=self._reader_loop, daemon=True)
            self._responses: queue.Queue[object] = queue.Queue()
            self._reader.start()
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
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _next_id(self) -> int:
        self.request_id += 1
        return self.request_id

    def _reader_loop(self) -> None:
        try:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "result" in message or "error" in message:
                    self._responses.put(message)
        finally:
            self._responses.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("MCP stdin is closed")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("MCP stdout is closed")
        response = self._responses.get(timeout=30)
        if response is None or not isinstance(response, dict):
            raise RuntimeError("perseus-vault closed the MCP stream")
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
        cleanup_error = None
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=30)
        except Exception as exc:
            cleanup_error = exc
            try:
                if os.name != "nt":
                    os.killpg(self.process.pid, signal.SIGTERM)
                else:
                    self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    if os.name != "nt":
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                    self.process.wait(timeout=5)
                except Exception as kill_exc:
                    cleanup_error = cleanup_error or kill_exc
        reader = getattr(self, "_reader", None)
        if reader is not None:
            reader.join(timeout=5)
            if reader.is_alive():
                raise RuntimeError("correction reader thread did not stop")
        if cleanup_error is not None and self.process.poll() is None:
            raise RuntimeError("correction client cleanup failed") from cleanup_error


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
    client = None
    rows: list[dict[str, Any]] = []

    def record(case_id: str, axis: str, ok: bool, detail: str = "") -> None:
        rows.append({"case": case_id, "axis": axis, "ok": bool(ok), "detail": detail})

    try:
        client = VaultClient(binary, db)
        for case in manifest["cases"]:
            category = case["category"]
            key = case["key"]
            remember(client, category, key, case["original"], valid_from_unix_ms=case["original"]["valid_from_unix_ms"])
            initial = items(client, case["current_query"], category)
            record(case["id"], "setup_original_readable", any(case["expected_history"] in body_blob(item) for item in initial))

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
        cleanup_errors = []
        if client is not None:
            try:
                client.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            shutil.rmtree(db.parent)
        except BaseException as exc:
            cleanup_errors.append(exc)
        if cleanup_errors:
            raise RuntimeError("correction benchmark cleanup failed") from cleanup_errors[0]

    passed = sum(row["ok"] for row in rows)
    if not rows:
        raise ValueError("correction benchmark produced no checks")
    raw_report = {
        "passed": passed == len(rows),
        "status": "passed" if passed == len(rows) else "failed",
        "capabilities": {"runner": {"status": "available"}},
        "network_calls": 0,
        "cases": [
            {
                "id": f"{row['case']}-{row['axis']}".lower(),
                "category": "correction",
                "status": "passed" if row["ok"] else "failed",
                "checks": {row["axis"]: bool(row["ok"])},
                "evidence": {"complete": True},
                **({"failure_class": "correction_check_failed"} if not row["ok"] else {}),
            }
            for row in rows
        ],
        "metrics": {"correction_durability": {"status": "available", "numerator": passed, "denominator": len(rows), "rate": passed / len(rows)}},
    }
    report = build_common_report(
        suite_id="perseus-vault-correction-durability",
        suite_version="v1",
        raw_report=raw_report,
        binary=binary,
        manifest=manifest,
        profile={"suite": "correction", "version": "v1", "network_calls": 0},
        repo_root=REPO,
        not_measured=[],
        claim_ids=["correction-v1-durability"],
        negative_claim_ids=["provider-failure-stress", "deletion-external-propagation"],
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"checks_passed": passed, "checks_total": len(rows), "accuracy": passed / len(rows), "out": args.out}, sort_keys=True))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
