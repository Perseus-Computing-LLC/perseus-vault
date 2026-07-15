#!/usr/bin/env python3
"""Regression gate for the #630 embedding-quantization ladder.

Asserts the measured, counterintuitive invariants that make aggressive index
quantization the right default — so a future re-run can't silently regress them:

  1. Denoising: the 1-bit prefilter's standalone dense recall@5 is NOT worse than
     full-precision exact cosine (it measured BETTER — the 1-bit code drops
     cross-cluster cosine confusers). Tolerance: >= exact - EPS.
  2. Latency ordering: more compression is faster — 1-bit p50 < int4 p50 < exact p50.
  3. Each measured tier cites a source artifact with a recorded SHA-256.

Exits non-zero on any violation. Reads the committed report.json (regenerate it
with aggregate.py first).

    python benchmark/embedding-quantization/gate.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EPS = 0.01  # recall tolerance: 1-bit must be within EPS below exact (it's above)

FAIL = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def main():
    report = json.loads((HERE / "report.json").read_text(encoding="utf-8"))
    ladder = {r["level"]: r for r in report["index_ladder"]}

    exact = ladder["full_f32_exact_cosine"]
    int4 = ladder["int4_sig4_coarse"]
    onebit = ladder["1bit_sig_prefilter_rerank"]

    # 1. Denoising — 1-bit dense recall@5 not materially worse than exact.
    check(
        onebit["dense"]["r@5"] >= exact["dense"]["r@5"] - EPS,
        "denoising: 1-bit dense r@5 (%.3f) >= exact (%.3f) - %.2f"
        % (onebit["dense"]["r@5"], exact["dense"]["r@5"], EPS),
    )

    # 2. Latency ordering — more compression is faster.
    check(
        onebit["dense"]["p50_ms"] < int4["dense"]["p50_ms"] < exact["dense"]["p50_ms"],
        "latency: 1-bit p50 (%.1f) < int4 (%.1f) < exact (%.1f) ms"
        % (onebit["dense"]["p50_ms"], int4["dense"]["p50_ms"], exact["dense"]["p50_ms"]),
    )

    # 3. Provenance — every measured tier cites a hashed source.
    for level, row in ladder.items():
        if "dense" in row:  # a measured tier
            src = row.get("source")
            hashed = src and report["provenance"].get(src, {}).get("sha256")
            check(bool(hashed), "provenance: %s cites a hashed source (%s)" % (level, src))

    if FAIL:
        print("\n%d gate check(s) FAILED" % len(FAIL))
        sys.exit(1)
    print("\nall gate checks passed")


if __name__ == "__main__":
    main()
