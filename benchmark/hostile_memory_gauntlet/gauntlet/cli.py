from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

from .acceptance import accept_run
from .evaluator import file_sha256, run_suite, write_json
from .protocol import canonical_json, sha256_text, validate_case_bundle, validate_manifest


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _load_provider(spec: str):
    try:
        module_name, symbol_name = spec.split(":", 1)
        module = importlib.import_module(module_name)
        provider_class = getattr(module, symbol_name)
    except (ValueError, ImportError, AttributeError) as exc:
        raise SystemExit(f"cannot load provider {spec}: {exc}") from exc
    return provider_class()


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    manifest_path = Path(args.manifest).resolve()
    manifest = _load_json(manifest_path)
    case_arg = args.cases or manifest.get("case_file")
    if not case_arg:
        raise SystemExit("manifest must specify case_file or --cases must be supplied")
    case_path = Path(case_arg)
    if not case_path.is_absolute():
        case_path = manifest_path.parent / case_path
    return manifest_path, case_path.resolve()


def _write_new(path: Path, value: dict[str, Any], *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing artifact: {path} (use --force)")
    write_json(path, value)


def run_command(args: argparse.Namespace) -> int:
    manifest_path, case_path = _paths(args)
    manifest = _load_json(manifest_path)
    bundle = _load_json(case_path)
    validate_manifest(manifest)
    validate_case_bundle(bundle, max_cases=manifest["config"].get("max_cases", 30))
    actual_case_hash = file_sha256(case_path)
    declared_case_hash = manifest.get("case_file_sha256")
    if declared_case_hash and declared_case_hash != actual_case_hash:
        raise SystemExit("case file hash does not match manifest")
    manifest_hash = sha256_text(canonical_json(manifest))
    provider = _load_provider(args.provider)
    try:
        run_return = run_suite(
            provider, manifest, bundle,
            case_file_sha256=actual_case_hash,
            manifest_sha256=manifest_hash,
            run_id=args.run_id,
        )
        output_path = Path(args.out).resolve()
        acceptance_path = Path(args.acceptance_out).resolve()
        _write_new(output_path, run_return, force=args.force)
        acceptance = accept_run(manifest, bundle, run_return, case_file_sha256=actual_case_hash)
        _write_new(acceptance_path, acceptance, force=args.force)
        print(json.dumps({
            "provider": run_return["provider"],
            "cases": run_return["case_count"],
            "probes": run_return["probe_count"],
            "verdict": run_return["verdict"],
            "acceptance_status": acceptance["acceptance_status"],
            "release_ready": acceptance["release_ready"],
            "run_return": str(output_path),
            "acceptance_report": str(acceptance_path),
        }, indent=2))
        return 0 if acceptance["acceptance_status"] == "accepted" else 2
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def accept_command(args: argparse.Namespace) -> int:
    manifest_path, case_path = _paths(args)
    manifest = _load_json(manifest_path)
    bundle = _load_json(case_path)
    run_return = _load_json(Path(args.run_return).resolve())
    actual_case_hash = file_sha256(case_path)
    acceptance = accept_run(manifest, bundle, run_return, case_file_sha256=actual_case_hash)
    output_path = Path(args.out).resolve()
    _write_new(output_path, acceptance, force=args.force)
    print(json.dumps({
        "acceptance_status": acceptance["acceptance_status"],
        "release_ready": acceptance["release_ready"],
        "acceptance_report": str(output_path),
    }, indent=2))
    return 0 if acceptance["acceptance_status"] == "accepted" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Perseus Hostile Memory Gauntlet")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="run a provider and emit run-return plus acceptance report")
    run.add_argument("--provider", default="gauntlet.providers:ReferenceProvider")
    run.add_argument("--manifest", default="fixtures/public_manifest.json")
    run.add_argument("--cases")
    run.add_argument("--out", default="artifacts/run-return.json")
    run.add_argument("--acceptance-out", default="artifacts/acceptance-report.json")
    run.add_argument("--run-id", default="local-control")
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=run_command)
    accept = sub.add_parser("accept", help="independently validate a run-return")
    accept.add_argument("--run-return", required=True)
    accept.add_argument("--manifest", default="fixtures/public_manifest.json")
    accept.add_argument("--cases")
    accept.add_argument("--out", default="artifacts/acceptance-report.json")
    accept.add_argument("--force", action="store_true")
    accept.set_defaults(func=accept_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    return int(args.func(args))
