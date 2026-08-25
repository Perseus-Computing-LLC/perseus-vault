#!/usr/bin/env python3
"""Real offline Perseus Vault MCP driver for the #1105 collector."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmark.quality.run import remember
from benchmark.resource_envelope import harness


class OfflineVaultClient:
    def __init__(self, binary: str, db: pathlib.Path, timeout_seconds: float = 30.0):
        self.client_name = "resource-envelope-v1"
        self.timeout_seconds = timeout_seconds
        self._id = 0
        self._queue: queue.Queue[Any] = queue.Queue()
        env = dict(os.environ)
        env["PERSEUS_VAULT_DISABLE_ADMISSION_LINT"] = "1"
        self.process = subprocess.Popen(
            [binary, "--db", str(db), "--offline"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=(os.name != "nt"),
            env=env,
        )
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()
        self._send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": "1"},
            },
        )
        self._read()
        self._notify("notifications/initialized")

    def _reader(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "result" in payload or "error" in payload:
                self._queue.put(payload)
        self._queue.put(None)

    def _next(self) -> int:
        self._id += 1
        return self._id

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Vault stdin is closed")
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _send(self, method: str, params: dict[str, Any]) -> int:
        request_id = self._next()
        self._write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        return request_id

    def _notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def _read(self) -> dict[str, Any]:
        try:
            payload = self._queue.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError("Vault MCP response timed out") from exc
        if payload is None:
            raise RuntimeError("Vault MCP stream closed")
        if "error" in payload:
            raise RuntimeError("Vault MCP request failed")
        return payload

    @staticmethod
    def _decode(payload: dict[str, Any]) -> Any:
        result = payload.get("result", {})
        if isinstance(result, dict) and result.get("isError"):
            raise RuntimeError("Vault MCP tool returned an error")
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content", []) if isinstance(result, dict) else []
        if content and isinstance(content[0], dict) and "text" in content[0]:
            text = content[0]["text"]
            try:
                return json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return text
        return result

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self._send("tools/call", {"name": name, "arguments": arguments or {}})
        return self._decode(self._read())

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=1)


def _counter_overhead_ns() -> int:
    samples = []
    for _ in range(101):
        start = time.perf_counter_ns()
        end = time.perf_counter_ns()
        samples.append(max(0, end - start))
    return sorted(samples)[len(samples) // 2]


def _extract_int(value: Any, names: set[str]) -> int | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key in names
                and isinstance(child, int)
                and not isinstance(child, bool)
                and child >= 0
            ):
                return child
        for child in value.values():
            found = _extract_int(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _extract_int(child, names)
            if found is not None:
                return found
    return None


def _status(recall: Any, item_count: int) -> str:
    if not isinstance(recall, dict):
        return "degraded"
    status = str((recall.get("outcome") or {}).get("status", "")).lower()
    if status in {"unavailable", "timeout", "partial", "degraded"}:
        return status
    if not item_count or status == "empty":
        return "empty"
    return "available"


def _remove_db_family(path: pathlib.Path) -> None:
    for candidate in (path, pathlib.Path(f"{path}-wal"), pathlib.Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def run_case(
    *,
    binary: str,
    manifest: dict[str, Any],
    case: dict[str, Any],
    repetition: int,
    state_dir: pathlib.Path,
) -> dict[str, Any]:
    normalized = harness.validate_manifest(manifest)
    cases = {item["id"]: item for item in normalized["cases"]}
    profiles = {item["id"]: item for item in normalized["profiles"]}
    corpora = {item["id"]: item for item in normalized["corpora"]}
    budgets = {item["id"]: item for item in normalized["budgets"]}
    frozen = cases.get(case.get("id"))
    provided = json.loads(json.dumps(case))
    if isinstance(provided.get("expected_outcomes"), list):
        provided["expected_outcomes"] = sorted(provided["expected_outcomes"])
    if frozen != provided:
        raise harness.ContractError("driver case does not match the frozen manifest")
    profile = profiles[case["profile_id"]]
    if (
        profile["deployment_profile"] != "offline"
        or profile["backend_runtime_manifest"]["network"] != "disabled"
    ):
        raise harness.ContractError("Vault driver accepts offline profiles only")
    binary_path = pathlib.Path(binary).resolve()
    if not binary_path.is_file():
        raise harness.ContractError("Vault binary does not exist")
    corpus = corpora[case["corpus_id"]]
    budget = budgets[case["budget_id"]]
    state_dir.mkdir(parents=True, exist_ok=True)
    db = state_dir / f"{case['id']}-{repetition}.db"
    _remove_db_family(db)
    client = OfflineVaultClient(str(binary_path), db)
    workspace = f"resource-{case['id']}-{repetition}"
    try:
        for index in range(corpus["entity_count"]):
            group = index % max(1, corpus["query_count"])
            remember(
                client,
                "resource_fixture",
                f"entity-{index:05d}",
                f"resource envelope marker group{group} deterministic entry {index}",
                workspace_hash=workspace,
                agent_id="resource-envelope-agent",
                requesting_agent_id=client.client_name,
            )
        query = "resource envelope marker group0"
        recall_args = {
            "query": query,
            "mode": "fused",
            "limit": budget["recall_limit"],
            "workspace_hash": workspace,
            "requesting_agent_id": client.client_name,
            "include_outcome": True,
            "include_selection_decisions": True,
            "multihop": budget["traversal_depth"] > 0,
        }
        context_args = {
            "query": query,
            "mode": "on_demand",
            "limit": budget["recall_limit"],
            "workspace_hash": workspace,
            "session_id": f"resource-{case['id']}",
            "max_context_chars": budget["context_char_budget"],
            "include_selection_decisions": True,
        }
        if case["phase"] == "warm":
            client.call("perseus_vault_recall", recall_args)
            client.call("perseus_vault_context", context_args)
        recall_started = time.perf_counter_ns()
        recall = client.call("perseus_vault_recall", recall_args)
        recall_ns = max(0, time.perf_counter_ns() - recall_started)
        context_started = time.perf_counter_ns()
        context = client.call("perseus_vault_context", context_args)
        context_ns = max(0, time.perf_counter_ns() - context_started)
        items = recall.get("items", []) if isinstance(recall, dict) else []
        if not isinstance(items, list):
            items = []
        context_bytes = len(
            json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
        )
        context_tokens = (
            max(0, int((context or {}).get("token_count", 0)))
            if isinstance(context, dict)
            else 0
        )
        if context_tokens == 0:
            context_tokens = (context_bytes + 3) // 4
        candidate_count = _extract_int(
            recall,
            {
                "candidate_count",
                "candidates_considered",
                "total_candidates",
                "scan_count",
            },
        )
        depth = _extract_int(recall, {"traversal_depth", "max_depth", "depth"})
        return {
            "outcome_status": _status(recall, len(items)),
            "network_calls": 0,
            "workload": {
                "measurement_overhead_ns": _counter_overhead_ns(),
                "recall_candidate_count": candidate_count
                if candidate_count is not None
                else len(items),
                "selected_count": len(items),
                "traversal_depth": depth if depth is not None else 0,
                "context_assembly_ns": context_ns,
                "output_bytes": context_bytes,
                "output_tokens": context_tokens,
            },
            "diagnostic": {"recall_round_trip_ns": recall_ns},
        }
    finally:
        client.close()
        _remove_db_family(db)
        try:
            shutil.rmtree(state_dir / f"{case['id']}-{repetition}")
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--case-json", required=True)
    parser.add_argument("--repetition", type=int, required=True)
    args = parser.parse_args()
    if os.environ.get("PERSEUS_RESOURCE_ENVELOPE_OFFLINE") != "1":
        raise SystemExit("offline collector marker is required")
    manifest = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    case = json.loads(args.case_json)
    result = run_case(
        binary=args.binary,
        manifest=manifest,
        case=case,
        repetition=args.repetition,
        state_dir=pathlib.Path(args.state_dir),
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
