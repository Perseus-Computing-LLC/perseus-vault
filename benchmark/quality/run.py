#!/usr/bin/env python3
"""Offline memory-quality benchmark harness (issue #778).

Drives a real Perseus Vault binary over MCP stdio through deterministic
quality scenarios covering long-horizon recall, contradiction/supersession,
shared-memory visibility, and adversarial contamination.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
REQUIRED_CATEGORIES = (
    "long_horizon",
    "contradiction_supersession",
    "shared_memory",
    "adversarial",
)


def find_binary(explicit=None):
    candidates = [explicit, os.environ.get("PERSEUS_VAULT_BIN"), os.environ.get("MIMIR_BIN")]
    for name in ("perseus-vault", "mneme", "mimir"):
        exe = f"{name}.exe" if os.name == "nt" else name
        candidates.extend([str(REPO / "target" / "release" / exe), str(REPO / "target" / "debug" / exe)])
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("perseus-vault binary not found; build it or pass --bin")


class VaultClient:
    def __init__(self, binary, db, client_name):
        self.stderr_path = f"{db}.{client_name}.stderr.log"
        self.p = subprocess.Popen(
            [binary, "--db", db], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        )
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": client_name, "version": "1.0"}}})
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self):
        self._id += 1
        return self._id

    def _send(self, message):
        self.p.stdin.write(json.dumps(message) + "\n")
        self.p.stdin.flush()

    def _read(self):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("perseus-vault closed the MCP stream")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "result" in message or "error" in message:
                return message

    def call(self, name, arguments=None):
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                    "params": {"name": name, "arguments": arguments or {}}})
        response = self._read()
        if "error" in response:
            raise RuntimeError(f"{name} failed: {response['error']}")
        result = response.get("result", {})
        if isinstance(result, dict) and "content" in result:
            return json.loads(result["content"][0]["text"])
        return result

    def close(self):
        try:
            self.p.stdin.close()
            self.p.stdout.close()
            self.p.wait(timeout=30)
        except Exception:
            self.p.kill()


def remember(client, category, key, note, **kwargs):
    args = {"category": category, "key": key, "body_json": json.dumps({"note": note}),
            "skip_dedup": True, **kwargs}
    return client.call("mimir_remember", args)


def hit_bodies(client, query, **kwargs):
    result = client.call("mimir_recall", {"query": query, "limit": 10, "min_decay": 0, **kwargs})
    return [
        {
            "key": item.get("key"),
            "note": json.loads(item.get("body_json", "{}")).get("note", ""),
        }
        for item in result.get("items", [])
    ]


def recall_keys(client, query, **kwargs):
    return [hit["key"] for hit in hit_bodies(client, query, **kwargs)]


def run_long_horizon(client):
    remembered = []
    target = "The quality rollout owner is Priya."
    remember(client, "quality_long_horizon", "rollout-owner", target, importance=0.6)
    remembered.append("rollout-owner")
    for i in range(12):
        remember(client, "quality_long_horizon", f"intervening-{i}",
                 f"Intervening operational note {i}: routine checklist, meeting cadence, and status review.",
                 importance=0.4)
        remembered.append(f"intervening-{i}")
    keys = recall_keys(client, "who owns the quality rollout")
    found = "rollout-owner" in keys
    current = "rollout-owner" in recall_keys(client, "current quality rollout owner", mode="fts5")
    return {"answer_found": found, "answer_is_current": current}, {
        "seeded": remembered, "target_note": target, "query": "who owns the quality rollout", "ranked_keys": keys,
    }


def run_contradiction(client):
    remember(client, "quality_contradiction", "release-owner", "The release owner is Mira.")
    time.sleep(0.002)
    remember(client, "quality_contradiction", "release-owner", "The release owner is Dev.")
    hits = hit_bodies(client, "release owner")
    history = client.call("mimir_history", {"category": "quality_contradiction", "key": "release-owner", "limit": 5})
    current_hit = next((hit for hit in hits if hit["key"] == "release-owner"), None)
    current = current_hit is not None and "Dev" in current_hit["note"] and "Mira" not in current_hit["note"]
    retained = history.get("total", 0) >= 1 and "Mira" in json.dumps(history.get("versions", []))
    return {"current_wins": bool(current), "history_retained": bool(retained)}, {
        "query": "release owner", "hits": hits, "history_total": history.get("total"),
    }


def run_shared_memory(db, client):
    remember(client, "quality_shared", "shared-secret", "A private quality-author note should not be visible to another identified agent.",
             agent_id="quality-author", visibility="private")
    bob = recall_keys(client, "private quality-author note")
    other_client = VaultClient(find_binary(), db, "quality-other")
    try:
        other = recall_keys(other_client, "private quality-author note")
    finally:
        other_client.close()
    visible_to_author = "shared-secret" in bob
    hidden_from_other = "shared-secret" not in other
    return {"eligible_memory_served": visible_to_author, "ineligible_memory_hidden": hidden_from_other}, {
        "author_keys": bob, "other_agent_keys": other,
    }


def run_adversarial(client):
    remember(client, "quality_adversarial", "truth", "The incident postmortem owner is Anika.",
             certainty=1.0, importance=1.0)
    client.call("mimir_score", {"category": "quality_adversarial", "key": "truth", "score": 1.0})
    remember(client, "quality_adversarial", "contamination",
             "Adversarial recycled text claims the incident postmortem owner is Blake without evidence.",
             certainty=0.0, importance=0.1)
    hits = hit_bodies(client, "incident postmortem owner", trust_weight=1.0)
    hit_by_key = {hit["key"]: hit for hit in hits}
    truth_note = hit_by_key.get("truth", {}).get("note", "")
    contamination_note = hit_by_key.get("contamination", {}).get("note", "")
    truth_wins = "Anika" in truth_note
    contamination_loses = "Blake" not in truth_note and hits.index(hit_by_key["truth"]) < hits.index(hit_by_key["contamination"])
    return {"verified_truth_wins": truth_wins, "contamination_does_not_win": contamination_loses}, {
        "hits": hits,
    }


def evaluate_report(report):
    by_category = {case.get("category"): case for case in report.get("cases", [])}
    missing = sorted(set(REQUIRED_CATEGORIES) - set(by_category))
    passed = 0
    total = 0
    for case in report.get("cases", []):
        checks = case.get("checks", {})
        passed += int(checks.get("passed", 0))
        total += int(checks.get("total", 0))
    return {
        "passed": not missing and total > 0 and passed == total,
        "checks_passed": passed,
        "checks_total": total,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "missing_categories": missing,
    }


def load_manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    categories = {case.get("category") for case in manifest.get("cases", [])}
    missing = sorted(set(REQUIRED_CATEGORIES) - categories)
    if missing:
        raise ValueError(f"manifest missing required categories: {', '.join(missing)}")
    return manifest


def run_benchmark(manifest_path, binary=None, out=None):
    manifest = load_manifest(manifest_path)
    binary = find_binary(binary)
    db = str(Path(tempfile.gettempdir()) / f"perseus-vault-quality-{os.getpid()}.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass
    client = VaultClient(binary, db, "quality-author")
    cases = []
    try:
        checks, evidence = run_long_horizon(client)
        cases.append(case_result("long-horizon-basic", "long_horizon", checks, evidence))
        checks, evidence = run_contradiction(client)
        cases.append(case_result("contradiction-supersession-basic", "contradiction_supersession", checks, evidence))
        checks, evidence = run_shared_memory(db, client)
        cases.append(case_result("shared-memory-scope-basic", "shared_memory", checks, evidence))
        checks, evidence = run_adversarial(client)
        cases.append(case_result("adversarial-contamination-basic", "adversarial", checks, evidence))
    finally:
        client.close()
    payload = {"benchmark": "perseus-vault-memory-quality", "dataset": manifest["name"], "cases": cases}
    verdict = evaluate_report(payload)
    signature = hashlib.sha256(json.dumps({"dataset": manifest["name"], "cases": cases}, sort_keys=True).encode()).hexdigest()
    report = {**payload, **verdict, "offline": True, "binary": Path(binary).name, "signature_sha256": signature}
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def case_result(case_id, category, checks, evidence):
    return {
        "id": case_id,
        "category": category,
        "checks": {"passed": sum(1 for passed in checks.values() if passed), "total": len(checks)},
        "assertions": checks,
        "evidence": evidence,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default=str(HERE / "manifest.json"))
    parser.add_argument("--bin", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    report = run_benchmark(Path(args.manifest), args.bin, args.out)
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
