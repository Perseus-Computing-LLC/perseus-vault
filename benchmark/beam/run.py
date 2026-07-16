#!/usr/bin/env python3
"""Perseus Vault BEAM harness — bi-temporal correctness + determinism AT SCALE (#685).

Named for BEAM (Beyond a Million Tokens, arXiv:2510.27246), which kills the
"dump everything in context" cheat by testing at 128K / 500K / 1M / 10M tokens.
Our claim is different from a context-window benchmark: FTS5 + deterministic
bi-temporal retrieval must not degrade as the corpus grows. So BEAM embeds the
CI-verified bi-temporal gauntlet (benchmark/temporal/gauntlet.py — 13 checks
across the three SQL:2011 axes) inside a filler corpus sized to each token tier
and asserts, at every tier:

  * correctness holds        — the gauntlet still scores 100% (13/13)
  * results are deterministic — two independent runs produce the identical
                                signature over PASS/FAIL verdicts
  * queries stay fast        — per-axis point-lookup latency p50/p95 at scale

This reuses gauntlet.run_scenarios / build_report verbatim (one source of
temporal truth), so BEAM can only ever agree with the gauntlet's own verdicts.

Fully offline (no network / API / LLM). Deterministic corpus (seeded). The
filler corpus is bi-temporally inert (all valid-since-creation, never
superseded), so it enlarges the search space without perturbing the scenarios,
which are keyed by their own (category, key).

Usage:
    cargo build --release
    # small tier locally (fast):
    python benchmark/beam/run.py --tiers 128K
    # full ladder (1M/10M are heavy — run on the GPU fleet, see benchmark/lambda/):
    python benchmark/beam/run.py --tiers 128K 500K 1M 10M --out report.json
    # validate the harness logic with no binary:
    python benchmark/beam/run.py --self-test

Exit code is non-zero if any tier fails correctness or determinism, so CI can
gate on it (see gate.py).
"""
import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
# Reuse the CI-verified bi-temporal logic rather than duplicating it.
sys.path.insert(0, str(REPO / "benchmark" / "temporal"))
import gauntlet  # noqa: E402

# Token tiers (BEAM). Value = approximate token budget for the filler corpus.
TIERS = {"128K": 128_000, "500K": 500_000, "1M": 1_000_000, "10M": 10_000_000}

# Rough token accounting: ~4 chars/token (GPT-style BPE average for English
# prose). Each filler entity below is ~this many chars of body text; we size the
# corpus to hit the tier's token budget. This is an ESTIMATE for corpus sizing,
# not a claim about a specific tokenizer — the number that matters is the entity
# count, which is reported exactly.
CHARS_PER_TOKEN = 4
_FILLER_TOPICS = [
    "deployment", "incident", "migration", "rollback", "latency", "capacity",
    "vendor", "contract", "onboarding", "retention", "encryption", "audit",
    "throughput", "quota", "backup", "failover", "schema", "index", "cache",
    "webhook",
]


def filler_body(i):
    """Deterministic, prose-ish filler body for entity i (~200 chars ≈ 50 tokens)."""
    t = _FILLER_TOPICS[i % len(_FILLER_TOPICS)]
    return {
        "note": (f"Operational record {i} concerning the {t} subsystem. "
                 f"Captured during routine review; no action required. "
                 f"Reference ticket OPS-{1000 + i}. Owner rotates weekly. "
                 f"This entry is bi-temporally inert filler for scale testing."),
        "topic": t,
        "seq": i,
    }


def tokens_for(n_entities):
    return int(n_entities * len(json.dumps(filler_body(0))) / CHARS_PER_TOKEN)


def entities_for_tier(token_budget):
    per = max(1, len(json.dumps(filler_body(0))) // CHARS_PER_TOKEN)
    return max(1, token_budget // per)


class PersistentVault:
    """One long-lived process; pipelines many tools/calls over a single stdio
    session. Used for bulk corpus population where gauntlet's process-per-call
    Vault would be pathologically slow. Reads are still done via gauntlet.Vault
    so BEAM exercises the exact call path the gauntlet does."""

    def __init__(self, binary, db):
        self.p = subprocess.Popen([binary, "--db", db], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        self._id = 0
        self._send({"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "beam", "version": "1"}}})
        self.p.stdout.readline()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _next(self):
        self._id += 1
        return self._id

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def call(self, name, args):
        rid = self._next()
        self._send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                    "params": {"name": name, "arguments": args}})
        return self.p.stdout.readline()  # response not parsed on the write path

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=120)
        except Exception:
            self.p.kill()


def populate(binary, db, n_entities, log):
    """Write n_entities bi-temporally-inert filler rows via one persistent
    process. skip_dedup=true so templated bodies are NOT collapsed by the
    near-duplicate merge (that merge is right for conversational memory, wrong
    for synthetic scale corpora)."""
    v = PersistentVault(binary, db)
    t0 = time.time()
    for i in range(n_entities):
        v.call("mimir_remember", {
            "category": "beam_filler", "key": f"f{i}",
            "body_json": json.dumps(filler_body(i)),
            "type": "fact", "skip_dedup": True,
        })
        if log and n_entities >= 10 and i % (n_entities // 10) == 0 and i:
            log(f"    populated {i}/{n_entities}")
    v.close()
    return time.time() - t0


def measure_latency(binary, db, samples=40):
    """Per-axis bi-temporal point-lookup latency (ms) against the loaded corpus,
    using gauntlet's exact process-per-call read path. Uses a scenario key that
    is guaranteed present (the gauntlet writes it), falling back to a filler key."""
    v = gauntlet.Vault(binary, db)
    now = gauntlet.now_ms()
    probes = {
        "as_of": ("mimir_as_of", {"category": "beam_filler", "key": "f0",
                                   "as_of_unix_ms": now}),
        "valid_at": ("mimir_valid_at", {"category": "beam_filler", "key": "f0",
                                        "valid_at_unix_ms": now}),
    }
    out = {}
    for axis, (tool, arg) in probes.items():
        times = []
        for _ in range(samples):
            s = time.time()
            v.call(tool, arg)
            times.append((time.time() - s) * 1000.0)
        times.sort()
        out[axis] = {
            "p50_ms": round(statistics.median(times), 2),
            "p95_ms": round(times[min(len(times) - 1, int(0.95 * len(times)))], 2),
            "samples": samples,
        }
    return out


def run_tier(binary, dataset, tier_name, token_budget, log):
    n = entities_for_tier(token_budget)
    db_dir = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
    db = str(db_dir / f"pv-beam-{tier_name}.db")
    for ext in ("", "-wal", "-shm"):
        try:
            os.remove(db + ext)
        except OSError:
            pass

    log(f"  [{tier_name}] populating ~{n} filler entities (~{tokens_for(n)} tokens)")
    pop_secs = populate(binary, db, n, log)

    # Run the CI-verified gauntlet embedded in the loaded corpus, twice, to
    # prove correctness AND determinism at this scale.
    v = gauntlet.Vault(binary, db)
    rep1 = gauntlet.build_report(gauntlet.run_scenarios(v, dataset), dataset, binary)
    rep2 = gauntlet.build_report(gauntlet.run_scenarios(v, dataset), dataset, binary)
    deterministic = rep1["signature_sha256"] == rep2["signature_sha256"]
    latency = measure_latency(binary, db)

    ok = rep1["accuracy"] == 1.0 and deterministic
    log(f"  [{tier_name}] accuracy={rep1['accuracy']*100:.1f}%  "
        f"deterministic={deterministic}  as_of p50={latency['as_of']['p50_ms']}ms")
    return {
        "tier": tier_name,
        "token_budget": token_budget,
        "filler_entities": n,
        "approx_tokens": tokens_for(n),
        "populate_secs": round(pop_secs, 2),
        "gauntlet_accuracy": rep1["accuracy"],
        "gauntlet_checks": f"{rep1['checks_passed']}/{rep1['checks_total']}",
        "deterministic": deterministic,
        "signature_sha256": rep1["signature_sha256"],
        "latency": latency,
        "ok": ok,
    }


def self_test():
    """Validate the pure-Python corpus/token logic without a binary (CI-cheap,
    and runnable where no built binary exists)."""
    assert entities_for_tier(TIERS["128K"]) > 0
    # entity count scales roughly linearly with the token budget
    small = entities_for_tier(TIERS["128K"])
    big = entities_for_tier(TIERS["1M"])
    assert big > small * 5, (small, big)
    # filler bodies are deterministic and distinct
    assert filler_body(1) != filler_body(2)
    assert filler_body(7) == filler_body(7)
    # token estimate is monotonic in entity count
    assert tokens_for(1000) > tokens_for(100)
    ds = json.loads((REPO / "benchmark" / "temporal" /
                     "gauntlet_dataset.json").read_text(encoding="utf-8"))
    assert len(ds["scenarios"]) >= 4, "gauntlet dataset must have >=4 scenarios"
    print("BEAM self-test OK — corpus/token logic and gauntlet dataset validated.")
    print(f"  128K -> {small} entities, 1M -> {big} entities, "
          f"{len(ds['scenarios'])} bi-temporal scenarios embedded per tier.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Perseus Vault BEAM at-scale bi-temporal benchmark")
    ap.add_argument("--bin", default=None)
    ap.add_argument("--tiers", nargs="+", default=["128K"],
                    help="subset of: " + " ".join(TIERS))
    ap.add_argument("--dataset", default=str(REPO / "benchmark" / "temporal" /
                                             "gauntlet_dataset.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--self-test", action="store_true",
                    help="validate harness logic without a binary, then exit")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    for t in args.tiers:
        if t not in TIERS:
            sys.exit(f"unknown tier '{t}'; choose from {' '.join(TIERS)}")

    binary = gauntlet.find_binary(args.bin)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))

    def log(m):
        print(m, flush=True)

    log(f"Perseus Vault BEAM — bi-temporal correctness + determinism at scale")
    log(f"  binary: {Path(binary).name}   tiers: {' '.join(args.tiers)}\n")

    tiers = [run_tier(binary, dataset, t, TIERS[t], log) for t in args.tiers]
    all_ok = all(t["ok"] for t in tiers)
    report = {
        "benchmark": "perseus-vault-beam",
        "reuses": "benchmark/temporal/gauntlet.py (run_scenarios/build_report)",
        "binary": Path(binary).name,
        "platform": platform.platform(),
        "offline": True,
        "tiers": tiers,
        "all_ok": all_ok,
    }
    out = args.out or str(HERE / "report.json")
    Path(out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print("\nBEAM summary:")
    for t in tiers:
        mark = "ok  " if t["ok"] else "FAIL"
        print(f"  [{mark}] {t['tier']:>4}  {t['filler_entities']:>9} entities  "
              f"acc={t['gauntlet_accuracy']*100:5.1f}%  det={t['deterministic']}  "
              f"as_of_p50={t['latency']['as_of']['p50_ms']}ms")
    print(f"\n-> {out}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
