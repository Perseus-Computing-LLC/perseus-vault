#!/usr/bin/env python3
"""Check source-derived MCP registry metadata without formatting-sensitive grep."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "mcp.rs"
source = SOURCE.read_text(encoding="utf-8")

# The registry is a raw JSON literal passed to serde_json::from_str. Extract the
# literal itself, then parse it exactly as the Rust implementation does. This
# remains insensitive to compact vs spaced JSON formatting.
match = re.search(r'r###"(\[.*?\])"###', source, flags=re.DOTALL)
if not match:
    raise SystemExit("could not locate embedded MCP registry literal")
registry = json.loads(match.group(1))
if not isinstance(registry, list):
    raise SystemExit("embedded MCP registry is not an array")

canonical: list[str] = []
for tool in registry:
    name = tool.get("name")
    if not isinstance(name, str):
        raise SystemExit("registry entry has no string name")
    if name.startswith("perseus_vault_"):
        name = "perseus_vault_" + name[len("perseus_vault_"):]
    canonical.append(name)

if len(set(canonical)) != len(canonical):
    raise SystemExit("canonical registry contains duplicate names")
if not all(name.startswith("perseus_vault_") for name in canonical):
    raise SystemExit("canonical registry contains a non-canonical name")

expected_count = len(canonical)
expected_all_count = expected_count * 3

# #1051 tool-scope classification: the TOOL_SCOPES side table in mcp.rs must
# be 1:1 with the canonical registry and use only the three known tiers.
scope_table = re.search(
    r"const TOOL_SCOPES: &\[\(&str, ToolScope\)\] = &\[(.*?)\];",
    source,
    flags=re.DOTALL,
)
if not scope_table:
    raise SystemExit("could not locate TOOL_SCOPES table in src/mcp.rs")
scope_entries = re.findall(
    r'"((?:perseus_vault_)[a-z0-9_]+)",\s*ToolScope::(Agent|Ops|Admin)',
    scope_table.group(1),
)
scope_names = [n for n, _ in scope_entries]
if len(set(scope_names)) != len(scope_names):
    raise SystemExit("TOOL_SCOPES contains duplicate names")
if sorted(scope_names) != sorted(canonical):
    raise SystemExit("TOOL_SCOPES is not 1:1 with the canonical registry")
scope_counts = {"agent": 0, "ops": 0, "admin": 0}
for _, tier in scope_entries:
    scope_counts[tier.lower()] += 1

# Current-facing metadata must carry the source-derived count. Historical
# evidence is intentionally excluded from this check.
metadata = {
    "README.md": ROOT / "README.md",
    "CLAIMS-AUDIT.md": ROOT / "CLAIMS-AUDIT.md",
    "glama.json": ROOT / "glama.json",
    "manifest.json": ROOT / "manifest.json",
    "server.json": ROOT / "server.json",
}
for label, path in metadata.items():
    text = path.read_text(encoding="utf-8")
    if label == "glama.json":
        value = json.loads(text).get("tools")
        if value != expected_count:
            raise SystemExit(f"{label}: tools={value}, expected {expected_count}")
    else:
        # README/claims use a direct count; structured product metadata uses
        # the same number in their descriptions.
        if label == "README.md" and f"{expected_count} canonical MCP tools" not in text:
            raise SystemExit(f"{label}: missing current registry count {expected_count}")
        if label == "CLAIMS-AUDIT.md" and f"{expected_count} canonical MCP tools" not in text:
            raise SystemExit(f"{label}: missing current registry count {expected_count}")
        if label == "manifest.json" and f"{expected_count} canonical MCP tools" not in text:
            raise SystemExit(f"{label}: missing current registry count {expected_count}")
        if label == "server.json" and f"{expected_count} canonical tools" not in text:
            raise SystemExit(f"{label}: missing current registry count {expected_count}")

# Release metadata must identify the same artifact that the package version
# claims. The publish workflow repairs this at runtime, but the checked-in
# server.json is itself a release input and must be truthful before publish.
cargo_text = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
cargo_version_match = re.search(r'^version\s*=\s*"([^"]+)"', cargo_text, flags=re.MULTILINE)
if not cargo_version_match:
    raise SystemExit("Cargo.toml: could not locate package version")
cargo_version = cargo_version_match.group(1)
server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
if server.get("version") != cargo_version:
    raise SystemExit(
        f"server.json: version={server.get('version')!r}, expected Cargo.toml version {cargo_version!r}"
    )
packages = server.get("packages")
if not isinstance(packages, list) or not packages:
    raise SystemExit("server.json: missing OCI package metadata")
oci_identifiers = [
    package.get("identifier")
    for package in packages
    if package.get("registryType") == "oci"
]
expected_oci = f"ghcr.io/perseus-computing-llc/perseus-vault:{cargo_version}"
if oci_identifiers != [expected_oci]:
    raise SystemExit(
        f"server.json: OCI identifiers={oci_identifiers!r}, expected [{expected_oci!r}]"
    )

print(json.dumps({
    "registry_count": expected_count,
    "canonical_tools_list_count": expected_count,
    "compatibility_manifest_count": expected_all_count,
    "scope_counts": scope_counts,
    "source": str(SOURCE.relative_to(ROOT)),
}, sort_keys=True))

if __name__ == "__main__":
    sys.exit(0)
