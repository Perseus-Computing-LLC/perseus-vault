#!/usr/bin/env python3
"""Fail if release metadata identifies different versions or OCI artifacts."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

cargo_text = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', cargo_text, re.MULTILINE)
if not match:
    raise SystemExit("Cargo.toml package version not found")
cargo_version = match.group(1)

manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
manifest_version = manifest.get("version")
if cargo_version != manifest_version:
    raise SystemExit(
        f"release metadata drift: Cargo.toml={cargo_version}, "
        f"manifest.json={manifest_version}"
    )

server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
server_version = server.get("version")
if cargo_version != server_version:
    raise SystemExit(
        f"release metadata drift: Cargo.toml={cargo_version}, "
        f"server.json={server_version}"
    )

packages = server.get("packages")
if not isinstance(packages, list) or not packages:
    raise SystemExit("server.json packages must be a non-empty list")
oci_identifiers = [
    package.get("identifier")
    for package in packages
    if isinstance(package, dict) and package.get("registryType") == "oci"
]
expected_oci = f"ghcr.io/perseus-computing-llc/perseus-vault:{cargo_version}"
if oci_identifiers != [expected_oci]:
    raise SystemExit(
        f"server.json OCI identifiers={oci_identifiers!r}, "
        f"expected [{expected_oci!r}]"
    )

print(json.dumps({
    "version": cargo_version,
    "manifest_version": manifest_version,
    "server_version": server_version,
    "oci_identifier": expected_oci,
}, sort_keys=True))
