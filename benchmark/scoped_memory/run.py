#!/usr/bin/env python3
"""Run and publish the portable scoped-memory contract (#1103)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from benchmark.package.common.artifacts import write_report
from benchmark.package.common.publication import build_common_report
from benchmark.scoped_memory.contract import (
    CONTRACT_VERSION,
    ContractRun,
    InProcessSurface,
    McpSurface,
    execute_contract,
    load_fixture,
)


def _case_passed(row: dict[str, object]) -> bool:
    checks = row.get("checks")
    return isinstance(checks, dict) and bool(checks) and all(bool(value) for value in checks.values())


def _raw_report(run: ContractRun, surface_name: str) -> dict[str, object]:
    passed_cases = sum(_case_passed(row) for row in run.outcomes.values())
    total_cases = len(run.outcomes)
    metric = (
        {"status": "available", "numerator": passed_cases, "denominator": total_cases}
        if run.passed
        else {"status": "unavailable" if run.projection["status"] == "unavailable" else "failed"}
    )
    return {
        "passed": run.passed,
        "status": "passed" if run.passed else "failed",
        "capabilities": {
            surface_name: {"status": "available" if run.projection["status"] != "unavailable" else "unavailable", "reason": "surface_unavailable"}
            if run.projection["status"] == "unavailable"
            else {"status": "available"},
        },
        "cases": [
            {
                "id": case_id,
                "category": "scoped_memory",
                "status": "passed" if _case_passed(row) else "failed",
                "checks": row["checks"],
                "evidence": {
                    "complete": _case_passed(row),
                    "digest": run.projection["projection_sha256"],
                    "evidence_hash": run.projection["receipt_sha256"],
                    "receipt_present": True,
                },
            }
            for case_id, row in sorted(run.outcomes.items())
        ],
        "metrics": {
            "scoped_memory_contract": metric,
        },
        "benchmark": "perseus-vault-scoped-memory-contract",
        "dataset": "synthetic-scoped-memory-fixture",
        "harness_version": CONTRACT_VERSION,
        "offline": True,
        "network_calls": 0,
        "required_categories": ["scoped_memory"],
    }


def publish_run(
    run: ContractRun,
    *,
    fixture: dict[str, object],
    surface_name: str,
    binary: str | Path,
    repo_root: str | Path = REPO,
) -> dict[str, object]:
    """Route the contract result through the existing common report boundary."""
    return build_common_report(
        suite_id="scoped-memory-contract",
        suite_version="v1",
        raw_report=_raw_report(run, surface_name),
        binary=binary,
        manifest=fixture,
        profile={"contract_version": CONTRACT_VERSION, "surface": surface_name, "scope_policy": "trusted-host-injected"},
        repo_root=repo_root,
        claim_ids=[],
        negative_claim_ids=[],
    )


def find_binary(explicit: str | None) -> Path:
    candidates = [explicit, os.environ.get("PERSEUS_VAULT_BIN")]
    executable = "perseus-vault.exe" if os.name == "nt" else "perseus-vault"
    candidates.extend((str(REPO / "target" / "debug" / executable), str(REPO / "target" / "release" / executable)))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise FileNotFoundError("checkout-built perseus-vault binary is required for --surface mcp")


def _run(args: argparse.Namespace, fixture: dict[str, object]) -> tuple[ContractRun, Path]:
    if args.surface == "inprocess":
        return execute_contract(InProcessSurface(), fixture=fixture), Path(__file__)
    binary = find_binary(args.bin)
    from benchmark.admission_fixture import child_env
    from integrations.client.perseus_vault_client import VaultClient

    with tempfile.TemporaryDirectory(prefix="scoped-memory-contract-") as directory:
        db = Path(directory) / "vault.db"
        with VaultClient(binary=str(binary), db_path=str(db), timeout=30, env=child_env(dict(os.environ))) as client:
            run = execute_contract(McpSurface(client), fixture=fixture)
    return run, binary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("inprocess", "mcp"), default="inprocess")
    parser.add_argument("--bin", default=None)
    parser.add_argument("--fixture", default=str(HERE / "fixture.json"))
    parser.add_argument("--out", default=str(HERE / "report.json"))
    args = parser.parse_args(argv)

    fixture = load_fixture(args.fixture)
    run, binary = _run(args, fixture)
    report = publish_run(run, fixture=fixture, surface_name=args.surface, binary=binary)
    write_report(args.out, report)
    print(json.dumps({"status": report["status"], "result_signature_sha256": report["result_signature_sha256"]}, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
