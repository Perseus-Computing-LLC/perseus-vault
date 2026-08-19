"""Zero-model corpus certification and deterministic redaction receipts.

This module is a benchmark packaging boundary, not a memory API.  It operates
on pinned Git objects and agent-visible fixture surfaces, never calls a model or
provider, and emits only bounded counts/digests plus relative paths in the
explicit redaction receipt.
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from typing import Any, Mapping, Iterable

CERTIFICATION_SCHEMA_VERSION = "perseus-vault-corpus-certification/v1"
REDACTION_SCHEMA_VERSION = "perseus-vault-corpus-redaction/v1"
_MATERIALIZATION_SCHEMA_VERSION = "perseus-vault-corpus-materialization/v1"
_UNCHECKED_ENV = "PERSEUS_VAULT_ALLOW_UNCHECKED_CORPUS"
_REQUIRED_SURFACES = frozenset({"source", "fixture", "evidence", "graph_identity", "challenge"})
_FINDING_CLASSES = (
    "auto-loaded-context",
    "suspicious-metadata",
    "benchmark-awareness",
    "patch-or-diff",
    "solution-leak",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_AUTO_CONTEXT_NAMES = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        ".cursorrules",
        ".windsurfrules",
        ".clinerules",
        ".cursor",
        ".claude",
        ".continue",
        ".github/copilot-instructions.md",
    }
)
_AUTO_CONTEXT_PARTS = frozenset({".cursor", ".claude", ".continue"})
_EXCLUDED_MATERIALIZATION_ROOTS = frozenset({".git", "target", "build", "dist", "node_modules", ".venv"})
_IDENTITY_FIELDS = frozenset({"id", "name", "label", "arm", "kind", "identity", "fixture_id"})
_BENCHMARK_MARKERS = (
    "benchmark",
    "longmemeval",
    "stele-bench",
    "no-memory",
    "memory-arm",
    "control-arm",
    "treatment-arm",
    "retrieval-replay",
    "gold-evidence",
)
_SUSPICIOUS_MARKERS = (
    ".git/",
    ".git\\",
    ".env",
    "credential",
    "api_key",
    "access_token",
    "authorization",
    "password",
    "secret",
)
_PATCH_MARKERS = ("diff --git", "*** begin patch", "--- a/", "+++ b/", "@@ ")
_SOLUTION_MARKERS = (
    "correct answer",
    "expected answer",
    "gold answer",
    "solution marker",
    "the answer is",
    "answer:",
)


class CorpusContractError(ValueError):
    """Raised when corpus packaging or certification fails closed."""


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CorpusContractError("value is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def _seal(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result[field] = sha256_text(stable_json(value))
    return result


def _require_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CorpusContractError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate_relative_path(value: Any) -> str:
    """Validate a portable relative POSIX path without normalizing it."""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CorpusContractError("relative path must be non-empty text")
    if "\\" in value or value.startswith("/") or _DRIVE_PATH.match(value):
        raise CorpusContractError("relative path must use portable POSIX components")
    if len(value) > 512:
        raise CorpusContractError("relative path is too long")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CorpusContractError("relative path contains an unsafe component")
    if str(PurePosixPath(value)) != value:
        raise CorpusContractError("relative path is not canonical")
    return value


def _is_redaction_path(relative: str) -> bool:
    parts = relative.split("/")
    if relative in _AUTO_CONTEXT_NAMES:
        return True
    if any(part in _AUTO_CONTEXT_PARTS for part in parts):
        return True
    return False


def _iter_files(root: Path) -> Iterable[tuple[str, Path]]:
    if not root.is_dir():
        raise CorpusContractError("corpus root is missing or not a directory")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        validate_relative_path(relative)
        if path.is_symlink():
            raise CorpusContractError(f"symlink is not allowed in corpus tree: {relative}")
        if path.is_file():
            yield relative, path


def tree_digest(root: Path) -> tuple[str, int, int]:
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for relative, path in _iter_files(root):
        data = path.read_bytes()
        digest = sha256_bytes(data)
        total_bytes += len(data)
        records.append({"path": relative, "bytes": len(data), "sha256": digest})
    return sha256_text(stable_json(records)), len(records), total_bytes


def _copy_tree(source: Path, destination: Path, *, redact: bool) -> list[dict[str, Any]]:
    if destination.exists():
        raise CorpusContractError("destination already exists")
    destination.mkdir(parents=True)
    removed: list[dict[str, Any]] = []
    for relative, path in _iter_files(source):
        if redact and _is_redaction_path(relative):
            data = path.read_bytes()
            removed.append({"path": relative, "bytes": len(data), "sha256": sha256_bytes(data)})
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return removed


def redact_tree(source: Path | str, destination: Path | str) -> dict[str, Any]:
    """Copy a candidate tree while removing known auto-loaded context files.

    Certification never calls this function implicitly.  That separation keeps
    inspection non-mutating and makes the redaction receipt independently
    auditable.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    source_digest, _, _ = tree_digest(source_path)
    removed = _copy_tree(source_path, destination_path, redact=True)
    redacted_digest, file_count, total_bytes = tree_digest(destination_path)
    base: dict[str, Any] = {
        "schema_version": REDACTION_SCHEMA_VERSION,
        "source_tree_sha256": source_digest,
        "redacted_tree_sha256": redacted_digest,
        "removed": sorted(removed, key=lambda item: item["path"]),
        "redacted_file_count": file_count,
        "redacted_bytes": total_bytes,
        "raw_inputs_captured": False,
    }
    receipt = _seal(base, "receipt_sha256")
    validate_redaction_receipt(receipt)
    return receipt


def validate_redaction_receipt(receipt: Any) -> None:
    if not isinstance(receipt, Mapping):
        raise CorpusContractError("redaction receipt must be an object")
    allowed = {
        "schema_version", "source_tree_sha256", "redacted_tree_sha256", "removed",
        "redacted_file_count", "redacted_bytes", "raw_inputs_captured", "receipt_sha256",
    }
    unknown = set(receipt) - allowed
    if unknown:
        raise CorpusContractError(f"redaction receipt contains unknown field: {sorted(unknown)[0]}")
    if receipt.get("schema_version") != REDACTION_SCHEMA_VERSION:
        raise CorpusContractError("unsupported redaction schema")
    _require_sha(receipt.get("source_tree_sha256"), "source_tree_sha256")
    _require_sha(receipt.get("redacted_tree_sha256"), "redacted_tree_sha256")
    _require_sha(receipt.get("receipt_sha256"), "receipt_sha256")
    if receipt.get("raw_inputs_captured") is not False:
        raise CorpusContractError("redaction receipt must declare raw_inputs_captured=false")
    for field in ("redacted_file_count", "redacted_bytes"):
        value = receipt.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CorpusContractError(f"{field} must be a non-negative integer")
    removed = receipt.get("removed")
    if not isinstance(removed, list):
        raise CorpusContractError("removed must be a list")
    seen: set[str] = set()
    for index, row in enumerate(removed):
        if not isinstance(row, Mapping) or set(row) != {"path", "bytes", "sha256"}:
            raise CorpusContractError(f"removed entry {index} is malformed")
        path = validate_relative_path(row["path"])
        if path in seen:
            raise CorpusContractError("removed paths must be unique")
        seen.add(path)
        _require_sha(row["sha256"], f"removed {index}.sha256")
        if isinstance(row["bytes"], bool) or not isinstance(row["bytes"], int) or row["bytes"] < 0:
            raise CorpusContractError(f"removed {index}.bytes must be non-negative")
    expected = sha256_text(stable_json({key: value for key, value in receipt.items() if key != "receipt_sha256"}))
    if expected != receipt["receipt_sha256"]:
        raise CorpusContractError("redaction receipt digest mismatch")


def _git_output(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.PIPE).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CorpusContractError("pinned Git object could not be resolved") from exc


def materialize_git_tree(repo: Path | str, treeish: str, destination: Path | str) -> dict[str, Any]:
    """Materialize tracked files from a pinned Git object into a Git-less tree."""
    repo_path = Path(repo)
    if not repo_path.is_dir() or not isinstance(treeish, str) or not treeish.strip():
        raise CorpusContractError("Git repository and pinned treeish are required")
    if not re.fullmatch(r"[A-Za-z0-9._:/^{}~-]{1,256}", treeish):
        raise CorpusContractError("treeish contains unsafe characters")
    tree_id = _git_output(repo_path, "rev-parse", f"{treeish}^{{tree}}")
    if not re.fullmatch(r"[0-9a-f]{40,64}", tree_id):
        raise CorpusContractError("resolved Git tree identity is malformed")
    try:
        archive = subprocess.check_output(["git", "archive", "--format=tar", treeish], cwd=repo_path, stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CorpusContractError("pinned Git tree could not be archived") from exc
    destination_path = Path(destination)
    if destination_path.exists():
        raise CorpusContractError("materialization destination already exists")
    destination_path.mkdir(parents=True)
    excluded: list[str] = []
    archive_file: tarfile.TarFile | None = None
    try:
        archive_file = tarfile.open(fileobj=BytesIO(archive), mode="r:")
        members = archive_file.getmembers()
        for member in members:
            name = member.name.rstrip("/")
            if not name:
                continue
            relative = validate_relative_path(name)
            root = relative.split("/", 1)[0]
            if root in _EXCLUDED_MATERIALIZATION_ROOTS:
                if relative not in excluded:
                    excluded.append(relative)
                continue
            target = destination_path / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isreg():
                raise CorpusContractError(f"non-regular Git archive member is not allowed: {relative}")
            source = archive_file.extractfile(member)
            if source is None:
                raise CorpusContractError(f"Git archive member could not be read: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
    finally:
        if archive_file is not None:
            archive_file.close()
    digest, file_count, total_bytes = tree_digest(destination_path)
    base: dict[str, Any] = {
        "schema_version": _MATERIALIZATION_SCHEMA_VERSION,
        "git_tree": tree_id,
        "materialized_tree_sha256": digest,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "excluded_roots": sorted(set(item.split("/", 1)[0] for item in excluded)),
        "raw_inputs_captured": False,
    }
    return _seal(base, "receipt_sha256")


def _surface_digest(value: Any, surface: str) -> tuple[str, int, int]:
    if surface == "source":
        if not isinstance(value, (str, Path)):
            raise CorpusContractError("source surface must be a directory")
        digest, count, total = tree_digest(Path(value))
        if count == 0:
            raise CorpusContractError("source surface is empty")
        return digest, count, total
    if value is None:
        raise CorpusContractError(f"{surface} surface is missing")
    if isinstance(value, str) and not value.strip():
        raise CorpusContractError(f"{surface} surface is empty")
    encoded = stable_json(value)
    return sha256_text(encoded), 1, len(encoded.encode("utf-8"))


def _add_finding(findings: list[dict[str, str]], surface: str, path: str, finding_class: str, marker: str) -> None:
    findings.append(
        {
            "surface": surface,
            "path": path,
            "class": finding_class,
            "marker": marker,
        }
    )


def _scan_text(
    text: str,
    *,
    surface: str,
    path: str,
    findings: list[dict[str, str]],
    identity_field: bool,
) -> None:
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTO_CONTEXT_NAMES if "/" not in marker and marker.lower() in lowered):
        _add_finding(findings, surface, path, "auto-loaded-context", "context-marker")
    if any(marker in lowered for marker in _SUSPICIOUS_MARKERS):
        _add_finding(findings, surface, path, "suspicious-metadata", "suspicious-marker")
    if any(marker in lowered for marker in _PATCH_MARKERS):
        _add_finding(findings, surface, path, "patch-or-diff", "patch-marker")
    if any(marker in lowered for marker in _SOLUTION_MARKERS):
        _add_finding(findings, surface, path, "solution-leak", "solution-marker")
    benchmark_markers: list[str] = list(_BENCHMARK_MARKERS)
    if surface == "fixture" or identity_field:
        benchmark_markers.append("fixture")
    if any(marker in lowered for marker in benchmark_markers):
        _add_finding(findings, surface, path, "benchmark-awareness", "benchmark-marker")


def _scan_value(
    value: Any,
    *,
    surface: str,
    path: str,
    findings: list[dict[str, str]],
    identity_field: bool = False,
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CorpusContractError(f"{surface} contains a non-text field name")
            child_path = f"{path}.{key}" if path else key
            _scan_value(
                child,
                surface=surface,
                path=child_path,
                findings=findings,
                identity_field=surface == "graph_identity" and key in _IDENTITY_FIELDS,
            )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _scan_value(child, surface=surface, path=f"{path}[{index}]", findings=findings, identity_field=identity_field)
    elif isinstance(value, str):
        _scan_text(value, surface=surface, path=path, findings=findings, identity_field=identity_field)
    elif isinstance(value, (int, float, bool)) or value is None:
        return
    else:
        raise CorpusContractError(f"{surface} contains unsupported value type")


def _scan_source(root: Path, findings: list[dict[str, str]]) -> None:
    for relative, path in _iter_files(root):
        lowered = relative.lower()
        if _is_redaction_path(relative):
            _add_finding(findings, "source", relative, "auto-loaded-context", "context-path")
        if any(marker in lowered for marker in (".git/", ".env", "credential", "token", "api_key")):
            _add_finding(findings, "source", relative, "suspicious-metadata", "metadata-path")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        _scan_text(text, surface="source", path=relative, findings=findings, identity_field=False)


def certify_surfaces(
    surfaces: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Certify every agent-visible surface without model/provider calls."""
    if not isinstance(surfaces, Mapping):
        raise CorpusContractError("surfaces must be an object")
    unknown = set(surfaces) - _REQUIRED_SURFACES
    if unknown:
        raise CorpusContractError(f"unknown surface: {sorted(unknown)[0]}")
    missing = sorted(_REQUIRED_SURFACES - set(surfaces))
    env = os.environ if environment is None else environment
    opt_out = str(env.get(_UNCHECKED_ENV, "")).lower() in {"1", "true", "yes", "on"}
    if missing:
        if not opt_out:
            raise CorpusContractError(f"missing required surface: {missing[0]}")
        base: dict[str, Any] = {
            "schema_version": CERTIFICATION_SCHEMA_VERSION,
            "status": "unchecked",
            "passed": False,
            "unchecked_opt_out": True,
            "missing_surface_count": len(missing),
            "missing_surface_digest": sha256_text(stable_json(missing)),
            "surface_digests": {},
            "finding_counts": {name: 0 for name in _FINDING_CLASSES},
            "raw_inputs_captured": False,
        }
        if manifest_sha256 is not None:
            _require_sha(manifest_sha256, "manifest_sha256")
            base["manifest_sha256"] = manifest_sha256
        else:
            base["manifest_sha256"] = sha256_text(stable_json({"missing": missing, "schema_version": CERTIFICATION_SCHEMA_VERSION}))
        receipt = _seal(base, "receipt_sha256")
        validate_certification_receipt(receipt)
        return receipt

    surface_digests: dict[str, str] = {}
    surface_stats: dict[str, dict[str, int]] = {}
    findings: list[dict[str, str]] = []
    for surface in sorted(_REQUIRED_SURFACES):
        digest, count, total = _surface_digest(surfaces[surface], surface)
        surface_digests[surface] = digest
        surface_stats[surface] = {"items": count, "bytes": total}
        if surface == "source":
            _scan_source(Path(surfaces[surface]), findings)
        else:
            _scan_value(surfaces[surface], surface=surface, path="", findings=findings)
    finding_counts = {name: 0 for name in _FINDING_CLASSES}
    for finding in findings:
        finding_counts[finding["class"]] += 1
    finding_commitment = sha256_text(stable_json(sorted(findings, key=lambda item: tuple(item.values()))))
    if manifest_sha256 is not None:
        _require_sha(manifest_sha256, "manifest_sha256")
        bound_manifest = manifest_sha256
    else:
        bound_manifest = sha256_text(stable_json({"schema_version": CERTIFICATION_SCHEMA_VERSION, "surface_digests": surface_digests}))
    base = {
        "schema_version": CERTIFICATION_SCHEMA_VERSION,
        "status": "passed" if not findings else "failed",
        "passed": not findings,
        "unchecked_opt_out": False,
        "manifest_sha256": bound_manifest,
        "surface_digests": surface_digests,
        "surface_stats": surface_stats,
        "finding_counts": finding_counts,
        "finding_sha256": finding_commitment,
        "raw_inputs_captured": False,
    }
    receipt = _seal(base, "receipt_sha256")
    validate_certification_receipt(receipt)
    return receipt


def validate_certification_receipt(receipt: Any) -> None:
    if not isinstance(receipt, Mapping):
        raise CorpusContractError("certification receipt must be an object")
    allowed = {
        "schema_version", "status", "passed", "unchecked_opt_out", "manifest_sha256",
        "surface_digests", "surface_stats", "missing_surface_count", "missing_surface_digest",
        "finding_counts", "finding_sha256", "raw_inputs_captured", "receipt_sha256",
    }
    unknown = set(receipt) - allowed
    if unknown:
        raise CorpusContractError(f"certification receipt contains unknown field: {sorted(unknown)[0]}")
    if receipt.get("schema_version") != CERTIFICATION_SCHEMA_VERSION:
        raise CorpusContractError("unsupported certification schema")
    if receipt.get("status") not in {"passed", "failed", "unchecked"}:
        raise CorpusContractError("invalid certification status")
    if not isinstance(receipt.get("passed"), bool) or not isinstance(receipt.get("unchecked_opt_out"), bool):
        raise CorpusContractError("certification status flags must be boolean")
    if receipt["status"] == "passed" and not receipt["passed"]:
        raise CorpusContractError("passed status contradicts passed=false")
    if receipt["status"] == "unchecked" and (receipt["passed"] or not receipt["unchecked_opt_out"]):
        raise CorpusContractError("unchecked status must remain an explicit non-pass")
    _require_sha(receipt.get("manifest_sha256"), "manifest_sha256")
    _require_sha(receipt.get("receipt_sha256"), "receipt_sha256")
    if receipt.get("raw_inputs_captured") is not False:
        raise CorpusContractError("receipt must declare raw_inputs_captured=false")
    surface_digests = receipt.get("surface_digests")
    if not isinstance(surface_digests, Mapping) or set(surface_digests) - _REQUIRED_SURFACES:
        raise CorpusContractError("surface digests are malformed")
    for surface, digest in surface_digests.items():
        _require_sha(digest, f"surface_digests.{surface}")
    counts = receipt.get("finding_counts")
    if not isinstance(counts, Mapping) or set(counts) != set(_FINDING_CLASSES):
        raise CorpusContractError("finding counts are incomplete")
    for finding_class in _FINDING_CLASSES:
        value = counts[finding_class]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CorpusContractError(f"finding count is invalid: {finding_class}")
    if "finding_sha256" in receipt:
        _require_sha(receipt["finding_sha256"], "finding_sha256")
    if receipt.get("receipt_sha256") != sha256_text(stable_json({k: v for k, v in receipt.items() if k != "receipt_sha256"})):
        raise CorpusContractError("certification receipt digest mismatch")
