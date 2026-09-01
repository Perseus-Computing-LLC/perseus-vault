#!/usr/bin/env python3
"""Run the official BEAM task protocol without publishing raw task data.

``adapter=fixture`` is the provider-free CI path. ``adapter=vault`` drives a
real ``perseus-vault`` binary against the same official chat/probing-question
files. Both paths produce the same hash-only retrieval artifacts. Answerer and
judge calls remain explicitly ``not_measured`` until a separately authorized
provider run supplies pinned model/prompt identities.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

try:
    from . import protocol
except ImportError:  # direct ``python benchmark/beam_task/run.py`` execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmark.beam_task import protocol


from benchmark.package.common.replay import prepare_recall_preflight, require_recall_items


class MCPServer:
    """Small JSON-RPC stdio client for the existing Vault binary contract."""

    def __init__(self, binary: str, db: str):
        self.process = subprocess.Popen(
            [binary, "--db", db],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._next_id = 0
        self._send({
            "jsonrpc": "2.0",
            "id": self._next(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "perseus-vault-beam-task", "version": "1"},
            },
        })
        self._read_result()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Vault MCP stdin is closed")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _read_result(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("Vault MCP stdout is closed")
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("Vault binary closed the MCP stream")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in message:
                raise RuntimeError(f"Vault MCP error: {message['error']}")
            if "result" in message:
                return message["result"]

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        self._send({
            "jsonrpc": "2.0",
            "id": self._next(),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        result = self._read_result()
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            self.process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=10)


class VaultAdapter:
    """Adapter that ingests one BEAM conversation into a temporary Vault DB."""

    def __init__(self, binary: str, db: str, *, category: str, mode: str):
        self.server = MCPServer(binary, db)
        self.category = category
        self.mode = mode

    def ingest(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            body = {key: message[key] for key in ("id", "role", "content", "time_anchor", "index") if key in message}
            self.server.call("perseus_vault_remember", {
                "category": self.category,
                "key": f"message-{message['id']}",
                "body_json": json.dumps(body, ensure_ascii=False, sort_keys=True),
                "type": "fact",
                "skip_dedup": True,
            })

    def retrieve(self, question: str, *, top_k: int) -> list[dict[str, Any]]:
        response = self.server.call("perseus_vault_recall", {
            "query": question,
            "category": self.category,
            "mode": self.mode,
            "limit": top_k,
            "trust_weight": 0,
            "min_decay": 0,
        })
        items = require_recall_items(response, limit=top_k)
        ranked: list[dict[str, Any]] = []
        for item in items:
            key = item.get("key") or item.get("id") or item.get("entity_id")
            content = item.get("body_json") or item.get("body") or item.get("content") or item.get("text")
            if isinstance(content, (dict, list)):
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            if not key or not content:
                # Keep the artifact complete only for rows that expose a
                # stable key and content; malformed provider rows are not
                # silently treated as successful retrievals.
                continue
            row = {"key": str(key), "content": str(content)}
            if item.get("score") is not None:
                semantics = item.get("score_semantics")
                if not isinstance(semantics, str) or not semantics:
                    raise ValueError("retrieval score requires explicit score_semantics")
                row["score"] = item["score"]
                row["score_semantics"] = semantics
            ranked.append(row)
        return ranked[:top_k]

    def close(self) -> None:
        self.server.close()


def _conversation_key(case: dict[str, Any]) -> tuple[str, str]:
    return str(case["size"]), str(case["conversation_id"])


def _adapter_for(*, adapter_name: str, messages: list[dict[str, Any]], binary: str | None,
                  size: str, conversation_id: str, temp_root: Path, mode: str) -> Any:
    if adapter_name == "fixture":
        return protocol.FixtureAdapter(messages)
    if adapter_name != "vault":
        raise ValueError(f"unknown adapter: {adapter_name}")
    if not binary:
        raise ValueError("--bin is required for the Vault adapter")
    category = re.sub(r"[^A-Za-z0-9._:-]", "-", f"beam-task-{size}-{conversation_id}")
    db = temp_root / f"{size}-{conversation_id}.db"
    return VaultAdapter(binary, str(db), category=category, mode=mode)


def run_dataset(*, data_root: str | Path, sizes: Iterable[str], source_revision: str,
                config: dict[str, Any] | None = None, adapter_name: str = "fixture",
                binary: str | None = None, output_dir: str | Path | None = None,
                conversation_ids: Iterable[str] | None = None,
                question_types: Iterable[str] | None = None,
                limit: int | None = None) -> dict[str, Any]:
    """Run retrieval for one or more official BEAM sizes.

    The runner never puts raw task text in ``report.json`` or the replay JSONL.
    A retrieval failure is represented as a failed case with a complete empty
    artifact, so it cannot be mistaken for a zero score or omitted denominator.
    """
    selected_sizes = list(sizes)
    config = copy.deepcopy(config or protocol.default_run_config())
    protocol.validate_run_config(config)
    root = Path(data_root)
    manifest = protocol.build_manifest(
        data_root=root,
        sizes=selected_sizes,
        source_revision=source_revision,
        retrieval=config["retrieval"],
        answerer=config["answerer"],
        judge=config["judge"],
        conversation_ids=conversation_ids,
        question_types=question_types,
    )
    all_cases: list[dict[str, Any]] = []
    for size in selected_sizes:
        all_cases.extend(protocol.load_cases(
            root,
            size=size,
            conversation_ids=conversation_ids,
            question_types=question_types,
            limit=None,
        ))
    if limit is not None:
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be positive")
        all_cases = all_cases[:limit]
    if not all_cases:
        raise ValueError("no BEAM cases selected")

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for case in all_cases:
        grouped.setdefault(_conversation_key(case), []).append(case)
    public_cases: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    top_k = int(config["retrieval"]["top_k"])
    mode = str(config["retrieval"]["mode"])
    config_sha256 = protocol.digest_manifest(config)
    code_sha256 = protocol.sha256_file(Path(protocol.__file__))
    retry = config["retry_policy"]

    with tempfile.TemporaryDirectory(prefix="beam-task-") as temporary:
        temp_root = Path(temporary)
        preflight_by_cell = {}
        for (size, conversation_id), group in grouped.items():
            if adapter_name == "vault":
                if not binary:
                    raise ValueError("--bin is required for the Vault adapter")
                category = re.sub(r"[^A-Za-z0-9._:-]", "-", f"beam-task-{size}-{conversation_id}")
                db = temp_root / f"{size}-{conversation_id}.db"
                preflight_by_cell[f"{size}:{conversation_id}"] = prepare_recall_preflight(
                    binary=binary,
                    db_path=str(db),
                    dataset={"manifest": manifest, "cases": group},
                    config={**config, "size": size, "conversation_id": conversation_id,
                            "category": category},
                    repo_root=str(Path(__file__).resolve().parents[2]),
                )
            adapter = _adapter_for(
                adapter_name=adapter_name,
                messages=group[0]["messages"],
                binary=binary,
                size=size,
                conversation_id=conversation_id,
                temp_root=temp_root,
                mode=mode,
            )
            try:
                cell_preflight = preflight_by_cell.get(f"{size}:{conversation_id}")
                if hasattr(adapter, "ingest"):
                    adapter.ingest(group[0]["messages"])
                for case in group:
                    attempt = protocol.call_with_retries(
                        lambda case=case: adapter.retrieve(case["question"], top_k=top_k),
                        max_attempts=retry["max_attempts"],
                        backoff_seconds=retry.get("backoff_seconds", 0.0),
                    )
                    if attempt["status"] == "ok":
                        raw_ranked = attempt["value"]
                        artifact = protocol.make_retrieval_artifact(
                            case,
                            raw_ranked,
                            top_k=top_k,
                            config_sha256=config_sha256,
                            code_sha256=code_sha256,
                            retrieval_profile="beam-task-v1",
                            mode=mode,
                            preflight=cell_preflight,
                        )
                        projected = protocol.project_case(case, artifact, status="retrieved")
                    else:
                        raw_ranked = []
                        artifact = protocol.make_retrieval_artifact(
                            case,
                            raw_ranked,
                            top_k=top_k,
                            config_sha256=config_sha256,
                            code_sha256=code_sha256,
                            retrieval_profile="beam-task-v1",
                            mode=mode,
                            preflight=cell_preflight,
                            status="unavailable",
                            reason="retrieval_attempt_failed",
                        )
                        projected = protocol.project_case(case, artifact, status="failed")
                        projected["retry"] = {
                            "status": "error",
                            "attempts": attempt["attempts"],
                            "error_class": attempt["error_class"],
                        }
                    public_cases.append(projected)
                    replay.append(artifact)
                    snapshots.append({
                        "cell_id": case["question_id"],
                        "snapshot": protocol.make_retrieval_snapshot(raw_ranked),
                    })
            finally:
                if hasattr(adapter, "close"):
                    adapter.close()

    evidence_classes = {
        "vault_measured": {
            "status": "measured" if adapter_name == "vault" else "not_measured",
            "scope": "retrieval_only",
        },
        "competitor_published": {"status": "not_measured"},
        "competitor_reproduced": {"status": "not_measured"},
    }
    # Failed retry details are useful custody evidence but are not allowed to
    # contain provider messages or raw input. Strip them from the report cases
    # and retain only aggregate counts in a stable report-level field.
    retry_errors = sum(1 for case in public_cases if case.get("status") == "failed")
    for case in public_cases:
        case.pop("retry", None)
    report = protocol.build_retrieval_report(
        manifest=manifest,
        config=config,
        cases=public_cases,
        evidence_classes=evidence_classes,
        preflight={"cells": {key: preflight_by_cell[key] for key in sorted(preflight_by_cell)}} if preflight_by_cell else None,
    )
    report["retry_errors"] = retry_errors
    report["adapter"] = adapter_name
    report["preflight"] = {"cells": {key: preflight_by_cell[key] for key in sorted(preflight_by_cell)}} if preflight_by_cell else None
    report["response_schema"] = (
        next(iter(preflight_by_cell.values()))["response_schema"]
        if preflight_by_cell else None
    )
    report["custody_sha256"] = protocol.sha256_text(protocol.stable_json({
        key: value for key, value in report.items() if key != "custody_sha256"
    }))

    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        replay.sort(key=lambda item: item["request"]["cell_id"])
        (destination / "retrieval_replay.jsonl").write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in replay),
            encoding="utf-8",
        )
        snapshots.sort(key=lambda item: item["cell_id"])
        (destination / "retrieval_snapshot.jsonl").write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in snapshots),
            encoding="utf-8",
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the official BEAM task retrieval lane")
    parser.add_argument("--data-root", required=True, help="BEAM checkout or fixture root")
    parser.add_argument("--size", nargs="+", default=["100K"], choices=list(protocol.SIZES))
    parser.add_argument("--source-revision", required=True, help="immutable 40-character BEAM source commit")
    parser.add_argument("--adapter", choices=["fixture", "vault"], default="vault")
    parser.add_argument("--bin", default=None, help="perseus-vault binary for --adapter vault")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--conversation-id", action="append", default=None)
    parser.add_argument("--question-type", action="append", default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--retrieval-mode", choices=["fts5", "dense", "hybrid"], default="hybrid")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    config = protocol.default_run_config()
    config["retrieval"]["top_k"] = args.top_k
    config["retrieval"]["mode"] = args.retrieval_mode
    report = run_dataset(
        data_root=args.data_root,
        sizes=args.size,
        source_revision=args.source_revision,
        config=config,
        adapter_name=args.adapter,
        binary=args.bin,
        output_dir=args.out_dir,
        conversation_ids=args.conversation_id,
        question_types=args.question_type,
        limit=args.limit,
    )
    print(json.dumps({
        "out_dir": args.out_dir,
        "adapter": report["adapter"],
        "status": report["status"],
        "cases": len(report["cases"]),
        "retry_errors": report["retry_errors"],
        "custody_sha256": report["custody_sha256"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
