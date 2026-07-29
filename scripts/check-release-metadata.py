#!/usr/bin/env python3
"""Fail if MCP bundle metadata drifts from the Cargo package version."""
import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
cargo = (root / "Cargo.toml").read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.MULTILINE)
if not match:
    raise SystemExit("Cargo.toml package version not found")
cargo_version = match.group(1)
manifest_version = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"]
if cargo_version != manifest_version:
    raise SystemExit(f"release metadata drift: Cargo.toml={cargo_version}, manifest.json={manifest_version}")
print(f"release metadata aligned: {cargo_version}")
