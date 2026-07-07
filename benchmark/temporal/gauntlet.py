#!/usr/bin/env python3
"""Perseus Vault bi-temporal GAUNTLET — the full SQL:2011 three-axis benchmark.

Where `run.py` proves the transaction-time axis with 4 fact-overwrite scenarios,
the gauntlet drives the real binary through the hard bi-temporal cases that
single-axis competitors get wrong:

  * transaction time   (as_of)      — "what did we BELIEVE at tx T"
  * valid/application   (valid_at)   — "what was TRUE in the world at valid T, per current knowledge"
  * full bi-temporal    (bitemporal) — "as of belief at tx_at, what was true at valid_at"

Scenarios: retroactive correction, proactive/future-dated facts,
belief-vs-truth divergence, out-of-order arrival, and closed periods.

Fully offline: no network, no API key, no LLM. Day-offset dataset is resolved
against a runtime anchor, so absolute times vary run-to-run but the PASS/FAIL
verdicts (and the signature over them) are stable for a correct implementation.

Usage:
    cargo build --release
    python benchmark/temporal/gauntlet.py
    python benchmark/temporal/gauntlet.py --bin /path/to/perseus-vault
    MIMIR_BIN=/path/to/binary python benchmark/temporal/gauntlet.py

Exit code is non-zero if any check fails, so CI can gate on it.
"""
import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DAY_MS = 86_400_000


def find_binary(explicit):
    cands = []
    if explicit:
        cands.append(explicit)
    if os.environ.get("MIMIR_BIN"):
        cands.append(os.environ["MIMIR_BIN"])
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        cands += [str(REPO / "target" / "release" / exe),
                  str(REPO / "target" / "debug" / exe)]
    for c in cands:
        if c and Path(c).exists():
            return str(Path(c).resolve())
    sys.exit("error: perseus-vault binary not found. Build it (`cargo build --release`) "
             "or pass --bin / set MIMIR_BIN.")


class Vault:
    """One MCP tools/call per process; state persists via the shared --db file."""

    def __init__(self, binary, db):
        self.binary, self.db = binary, db

    def call(self, name, args):
        p = subprocess.Popen([self.binary, "--db", self.db], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        w = p.stdin.write
        w(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "gauntlet", "version": "1"}}}) + "\n")
        p.stdin.flush()
        p.stdout.readline()
        w(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        p.stdin.flush()
        w(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": name, "arguments": args}}) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        p.stdin.close()
        p.wait(timeout=120)
        resp = json.loads(line)
        r = resp.get("result", {})
        if isinstance(r, dict) and "content" in r:
            try:
                return json.loads(r["content"][0]["text"])
            except Exception:
                return r["content"][0]["text"]
        return resp


def now_ms():
    return int(time.time() * 1000)


def main():
    ap = argparse.ArgumentParser(description="Perseus Vault bi-temporal gauntlet")
    ap.add_argument("--bin", default=None)
    ap.add_argument("--dataset", default=str(HERE / "gauntlet_dataset.json"))
    ap.add_argument("--out", default=str(HERE / "gauntlet_report.json"))
    args = ap.parse_args()

    binary = find_binary(args.bin)
    data = json.loads(Path(args.dataset).read_text(encoding="utf-8"))

    db_dir = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    db = str(db_dir / "pv-gauntlet.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass
    v = Vault(binary, db)

    checks = []

    def record(scn, name, ok, detail=""):
        checks.append({"scenario": scn, "check": name, "ok": bool(ok), "detail": detail})

    for scn in data["scenarios"]:
        anchor = now_ms()  # per-scenario anchor so day-offsets are self-consistent
        cat, key = scn["category"], scn["key"]
        tx_marks = {}

        for wr in scn["writes"]:
            body = json.dumps(wr["body"])
            rargs = {"category": cat, "key": key, "body_json": body, "type": "fact",
                     "valid_from_unix_ms": anchor + wr["valid_from_days"] * DAY_MS}
            if wr.get("valid_to_days") is not None:
                rargs["valid_to_unix_ms"] = anchor + wr["valid_to_days"] * DAY_MS
            if wr.get("recorded_offset_ms"):
                # separate writes in transaction time so as_of can distinguish beliefs
                time.sleep(wr["recorded_offset_ms"] / 1000.0)
            v.call("mimir_remember", rargs)
            if wr.get("mark_tx"):
                tx_marks[wr["mark_tx"]] = now_ms()

        tx_marks["after"] = now_ms() + 5000  # comfortably after every write

        for chk in scn["checks"]:
            axis = chk["axis"]
            if axis == "valid_at":
                res = v.call("mimir_valid_at",
                             {"category": cat, "key": key,
                              "valid_at_unix_ms": anchor + chk["at_days"] * DAY_MS})
            elif axis == "as_of":
                res = v.call("mimir_as_of",
                             {"category": cat, "key": key,
                              "as_of_unix_ms": tx_marks[chk["tx_mark"]]})
            elif axis == "bitemporal":
                res = v.call("mimir_bitemporal",
                             {"category": cat, "key": key,
                              "tx_at_unix_ms": tx_marks[chk["tx_mark"]],
                              "valid_at_unix_ms": anchor + chk["valid_at_days"] * DAY_MS})
            else:
                record(scn["id"], f"{axis}:unknown", False, "unknown axis")
                continue

            blob = json.dumps(res) if isinstance(res, (dict, list)) else str(res)
            found = isinstance(res, dict) and res.get("found", False)
            label = f"{axis}[{chk.get('at_days', chk.get('valid_at_days',''))}"
            label += f"@{chk.get('tx_mark','')}]" if chk.get("tx_mark") else "]"

            if chk.get("expect_found") is False:
                ok = isinstance(res, dict) and res.get("found") is False
                record(scn["id"], label + " -> not-found", ok,
                       "" if ok else f"expected found=false, got {blob[:120]}")
            else:
                want = chk["expect_body_contains"]
                ok = found and want in blob
                record(scn["id"], label + f" -> {want}", ok,
                       "" if ok else f"expected '{want}', got {blob[:160]}")

    total = len(checks)
    passed = sum(1 for c in checks if c["ok"])
    by_axis = {}
    for c in checks:
        axis = c["check"].split("[")[0].split(":")[0].split(" ")[0]
        b = by_axis.setdefault(axis, {"pass": 0, "total": 0})
        b["total"] += 1
        b["pass"] += 1 if c["ok"] else 0
    accuracy = round(passed / total, 4) if total else 1.0

    sig_payload = json.dumps(
        {"dataset": data.get("name"),
         "checks": [{"s": c["scenario"], "c": c["check"], "ok": c["ok"]} for c in checks]},
        sort_keys=True)
    signature = hashlib.sha256(sig_payload.encode("utf-8")).hexdigest()

    report = {
        "benchmark": "perseus-vault-bitemporal-gauntlet",
        "dataset": data.get("name"),
        "n_scenarios": len(data["scenarios"]),
        "checks_total": total,
        "checks_passed": passed,
        "accuracy": accuracy,
        "by_axis": by_axis,
        "binary": Path(binary).name,
        "platform": platform.platform(),
        "offline": True,
        "signature_sha256": signature,
        "results": checks,
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Perseus Vault bi-temporal gauntlet — {data.get('name')}")
    print(f"  {len(data['scenarios'])} scenarios, {total} checks across 3 temporal axes\n")
    for axis, b in sorted(by_axis.items()):
        mark = "ok  " if b["pass"] == b["total"] else "FAIL"
        print(f"  [{mark}] {b['pass']}/{b['total']}  {axis}")
    print()
    for c in checks:
        status = "PASS" if c["ok"] else "MISS"
        line = f"  [{status}] {c['scenario']}: {c['check']}"
        if not c["ok"] and c["detail"]:
            line += f"\n         {c['detail']}"
        print(line)
    print(f"\naccuracy: {accuracy*100:.1f}%   signature: {signature[:16]}...  ->  {args.out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
