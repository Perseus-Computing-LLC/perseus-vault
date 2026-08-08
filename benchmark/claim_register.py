#!/usr/bin/env python3
"""Validate the benchmark claim register's publication boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ALLOWED = {"supported", "review", "not_measured", "blocked", "superseded"}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("claim_register.json"))
    register = json.loads(path.read_text())
    if register.get("schema_version") != "perseus-vault-benchmark-claims/v1":
        raise SystemExit("unsupported claim register schema")
    if register.get("policy", {}).get("composite_score") is not False:
        raise SystemExit("composite score policy must be false")
    claims = register.get("claims")
    if not isinstance(claims, list) or not claims:
        raise SystemExit("claim register must contain claims")
    ids = set()
    for claim in claims:
        if not isinstance(claim, dict) or not claim.get("id") or claim["id"] in ids:
            raise SystemExit("claim IDs must be unique")
        ids.add(claim["id"])
        if claim.get("status") not in ALLOWED:
            raise SystemExit(f"invalid claim status: {claim.get('status')}")
        if claim["status"] in {"not_measured", "blocked"} and not claim.get("negative_claim"):
            raise SystemExit(f"{claim['id']} needs a negative_claim")
        if claim["status"] == "supported" and not claim.get("evidence"):
            raise SystemExit(f"{claim['id']} needs evidence")
    print(f"CLAIM REGISTER OK ({len(claims)} claims)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
