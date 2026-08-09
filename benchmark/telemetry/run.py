#!/usr/bin/env python3
"""Bounded offline retrieval-telemetry benchmark harness (#872).

Drives a checkout-built Perseus Vault binary over MCP stdio, seeds a temp
database with the #872 acceptance fixtures (verified vs low-trust evidence,
repeated serving, superseded/quarantined contamination, diversity halving),
exercises real recall paths (lexical / hybrid / fused), then calls
``perseus_vault_retrieval_telemetry`` and asserts the acceptance invariants:

  * concentration: verified evidence ranks above a high-similarity low-trust
    lookalike (top-1 = verified id);
  * repeated serving / fanout: repeat_rate > 0 and low-trust fanout across
    query classes is reported;
  * contamination: superseded + quarantined entities never appear in any
    delivered set, served_reentry == 0, probe invariant fails closed
    (blocked re-entry counted);
  * displacement: diversity-halving quota drops are recorded (sole-evidence
    flagged when the keyword loses its last representative);
  * state separation: empty window reports "empty" (never a zero
    concentration misread), and a scoped report filters by workspace.

Report stays small and replayable: counts + identity keys + content hashes;
prompts and tool argument payloads are deliberately not retained.

Usage:
    python3 benchmark/telemetry/run.py [--bin /path/to/perseus-vault] [--db /tmp/x.db]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

MCP_RESPONSE_TIMEOUT_SECONDS = 60.0

FIXTURES = [
    # verified evidence for the concentration fixture. `verified` itself is
    # not settable via the MCP remember tool (it arrives through admission
    # events) — the benchmark exercises the same trust axis through
    # certainty: vt-1 (0.95) must outrank the verbatim low-trust lookalike
    # lt-1 (0.1) under trust_weight ranking.
    {"id": "vt-1", "category": "facts", "key": "vt-1",
     "body": {"note": "quark fusion reactor temperature report"},
     "certainty": 0.95},
    {"id": "lt-1", "category": "facts", "key": "lt-1",
     "body": {"note": "quark fusion reactor"},
     "certainty": 0.1},
    # repeated-serving / fanout fixture: one low-trust entity served across
    # three distinct query classes plus a repeat.
    {"id": "rp-1", "category": "facts", "key": "rp-1",
     "body": {"note": "alpha beta gamma config"},
     "certainty": 0.2},
    # contamination fixtures: superseded + quarantined variants of the same
    # fact must never be delivered by any arm.
    {"id": "c1", "category": "facts", "key": "c1",
     "body": {"note": "zeppelin core notes current"}, "status": "active"},
    {"id": "c2", "category": "facts", "key": "c2",
     "body": {"note": "zeppelin core notes superseded variant"}, "status": "deprecated"},
    {"id": "c3", "category": "facts", "key": "c3",
     "body": {"note": "zeppelin core notes quarantined variant"}, "status": "quarantined"},
    # diversity displacement fixtures: dz-2/dz-3 match ONLY via "core" (no
    # "zeppelin"), so the halving quota cutting at limit drops them with
    # their keyword unrepresented -> sole-evidence displacement.
    {"id": "dz-0", "category": "facts", "key": "dz-0",
     "body": {"note": "zeppelin config alpha"}, "status": "active"},
    {"id": "dz-1", "category": "facts", "key": "dz-1",
     "body": {"note": "zeppelin routing beta"}, "status": "active"},
    {"id": "dz-2", "category": "facts", "key": "dz-2",
     "body": {"note": "core hardening checklist"}, "status": "active"},
    {"id": "dz-3", "category": "facts", "key": "dz-3",
     "body": {"note": "core provisioning notes"}, "status": "active"},
    # workspace-scoped fixture.
    {"id": "sc-1", "category": "facts", "key": "sc-1",
     "body": {"note": "scope config vault"}, "workspace_hash": "ws-abc"},
]


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Vault:
    """Persistent MCP stdio client — one process, many calls."""

    def __init__(self, binary: str, db: str):
        self.p = subprocess.Popen(
            [binary, "--db", db],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._id = 0
        self._send(
            {"jsonrpc": "2.0", "id": self._n(), "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "telemetry-bench", "version": "1.0"}}}
        )
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _n(self):
        self._id += 1
        return self._id

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _read(self):
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("vault process exited prematurely")
        return json.loads(line)

    def call(self, name, args):
        self._send({"jsonrpc": "2.0", "id": self._n(), "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
        msg = self._read()
        if "error" in msg:
            raise RuntimeError(f"{name} failed: {msg['error']}")
        result = msg.get("result", {})
        content = result.get("content", [])
        for part in content:
            if part.get("type") == "text":
                return part["text"]
        return json.dumps(result)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def remember(v, fx):
    args = {"category": fx["category"], "key": fx["key"],
            "body_json": json.dumps(fx["body"])}
    if fx.get("certainty") is not None:
        args["certainty"] = fx["certainty"]
    if fx.get("status"):
        args["status"] = fx["status"]
    if fx.get("workspace_hash"):
        args["workspace_hash"] = fx["workspace_hash"]
    v.call("perseus_vault_remember", args)


def recall(v, query, mode=None, **kw):
    args = {"query": query}
    if mode is not None:
        args["mode"] = mode
    args.update(kw)
    return v.call("perseus_vault_recall", args)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=os.environ.get("PERSEUS_VAULT_BIN"))
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    binary = args.bin
    if not binary:
        for cand in (REPO / "target" / "release" / "perseus-vault",
                     REPO / "target" / "debug" / "perseus-vault"):
            if cand.exists():
                binary = str(cand)
                break
    if not binary:
        print("error: perseus-vault binary not found; pass --bin", file=sys.stderr)
        return 2

    tmpdir = tempfile.mkdtemp(prefix="vault-tel-bench-")
    db = args.db or str(Path(tmpdir) / "bench.db")
    v = Vault(binary, db)
    try:
        for fx in FIXTURES:
            remember(v, fx)

        # 1) concentration: trust-weighted fts5 recall.
        parsed = json.loads(recall(v, "quark fusion reactor", "fts5", limit=10, trust_weight=1.0))
        items1 = parsed.get("items") or (parsed if isinstance(parsed, list) else [])
        r1_keys = [e["key"] for e in items1]
        vt1_id = next((e["id"] for e in items1 if e["key"] == "vt-1"), None)

        # Concentration window: report BEFORE the repeat/fanout recalls so
        # the top entity is vt-1, not the later over-served rp-1.
        rep0 = json.loads(v.call("perseus_vault_retrieval_telemetry", {"since_hours": 24}))
        assert rep0["state"] in ("ok", "degraded"), rep0["state"]
        assert rep0["concentration"]["top_entity_id"] == vt1_id, rep0["concentration"]

        # 2) repeated serving + fanout: alpha twice, beta, gamma.
        recall(v, "alpha config")
        recall(v, "alpha config")
        recall(v, "beta config")
        recall(v, "gamma config")

        # 3) contamination: superseded/quarantined must never be delivered.
        exercised_modes = ["fts5"]
        for mode in ("fts5", "hybrid", "fused"):
            try:
                r = json.loads(recall(v, "zeppelin core", mode, limit=10))
            except RuntimeError as exc:
                # hybrid/fused can be unavailable when no embedding backend
                # is configured; degrade gracefully and note it.
                print(f"note: {mode} recall unavailable: {exc}")
                continue
            exercised_modes.append(mode)
            ids = [e["key"] for e in (r.get("items") or (r if isinstance(r, list) else []))]
            assert "c2" not in ids and "c3" not in ids, f"{mode} leaked {ids}"

        # 4) diversity displacement (offset widens the fetch so the quota
        #    sees the core-only entities; pagination window stays sane).
        recall(v, "zeppelin core", "fts5", limit=2, offset=1, diversity_halving=0.5)

        # 5) state separation + probe (opt-in via probe_query).
        t0 = v.call("perseus_vault_retrieval_telemetry",
                    {"since_hours": 24, "probe_query": "zeppelin core", "probe_mode": "fused"})
        rep = json.loads(t0)
        assert rep["state"] in ("ok", "degraded"), rep["state"]
        scoped = json.loads(v.call(
            "perseus_vault_retrieval_telemetry",
            {"since_hours": 24, "workspace_hash": "ws-zzz"}))
        assert scoped["state"] in ("empty", "ok"), scoped["state"]
        scoped_abc = json.loads(v.call(
            "perseus_vault_retrieval_telemetry",
            {"since_hours": 24, "workspace_hash": "ws-abc"}))
        assert scoped_abc["denominator"]["recalls"] >= 1, scoped_abc

        # ── assertions ────────────────────────────────────────────────
        assert r1_keys and r1_keys[0] == "vt-1", f"concentration violated: {r1_keys}"
        rr = rep["repeated_serving"]
        assert rr["repeat_rate"] > 0.0, rr
        fan = rep["fanout_low_trust"]
        assert any(e["query_classes"] >= 3 for e in fan), fan
        cont = rep["contamination"]
        assert cont["served_reentry"] == 0, cont
        assert cont["probe"] is not None, "probe did not run"
        assert cont["probe"]["invariant"] is False, cont["probe"]
        probe_blocked = sum(a["blocked_reentry"] for a in cont["probe"]["arms"])
        assert probe_blocked >= 2, cont["probe"]
        assert cont["arm_audits"], "no arm audits recorded"
        modes = set(a["mode"] for a in cont["arm_audits"])
        assert "lexical" in modes, modes
        for m in ("hybrid", "fused"):
            if m in exercised_modes:
                assert m in modes, f"{m} exercised but not audited: {modes}"
        assert rep["displacement"]["count"] >= 1, rep["displacement"]
        art = rep["artifact"]
        assert art.get("schema_version") == 32 and art.get("content_hash"), art

        # 6) #870: deployment profile joins the run manifest — the runtime
        # posture the results were produced under (sanitized: hosts only).
        prof = json.loads(v.call("perseus_vault_deployment_profile", {}))
        assert prof["profile"] in ("offline", "local_only", "local_with_approved_network",
                                   "external_actions_enabled"), prof

        # replayable report: counts + ids + hashes only.
        report = {
            "state": rep["state"],
            "concentration": {"top_entity_key": r1_keys[0],
                              "top_entity_id": rep0["concentration"]["top_entity_id"],
                              "herfindahl": round(rep0["concentration"]["herfindahl"], 4)},
            "repeated_serving": {"repeat_rate": round(rr["repeat_rate"], 4)},
            "low_trust_fanout_max_classes": max(e["query_classes"] for e in fan),
            "contamination": {"served_reentry": cont["served_reentry"],
                              "probe_blocked": probe_blocked,
                              "arm_audit_modes": sorted(modes),
                              "exercised_modes": sorted(exercised_modes)},
            "displacement": {"count": rep["displacement"]["count"],
                             "sole_evidence_sample": rep["displacement"]["sample"][:3]},
            "retrieval_profile": rep["retrieval_profile"],
            "artifact": {"schema_version": art["schema_version"],
                         "content_hash": art["content_hash"][:16]},
            "deployment_profile": {
                "profile": prof["profile"],
                "model_backend": prof["model_backend"]["kind"],
                "embedding_backend": prof["embedding_backend"]["kind"],
                "egress_hosts": prof["network"]["egress_hosts"],
                "external_mutations": prof["external_mutations"],
                "encryption_at_rest": prof["encryption"]["at_rest"],
            },
            "assertions_passed": True,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    finally:
        v.close()


if __name__ == "__main__":
    sys.exit(main())
