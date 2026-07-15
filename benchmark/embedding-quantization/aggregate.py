#!/usr/bin/env python3
"""Aggregate the embedding-quantization ladder for #630 into a signed report.

This benchmark has TWO orthogonal quantization axes:

  1. INDEX / SIGNATURE quantization — how a stored embedding is COMPRESSED for
     the dense prefilter. The engine keeps three tiers of the same vector:
       * full f32   (`embedding`)  — exact cosine, 32 bits/dim   (1x)
       * int4       (`emb_sig4`)   — 4-bit ADC signature,  4 bits/dim (8x)
       * 1-bit      (`emb_sig`)    — sign signature, Hamming, 1 bit/dim (32x)
     This axis is MEASURED at 1M on the #619 Lambda runs and is what this script
     aggregates — the rows are extracted verbatim from those signed artifacts.

  2. MODEL-WEIGHT quantization — how the embedding is GENERATED. The shipped
     bundled LOCAL default is all-MiniLM-L6-v2 **qint8** (384-dim), used for the
     offline/default path and the repo's 100K + LongMemEval numbers. NOTE: the
     1M index ladder in axis 1 was measured on the nomic-768 ENDPOINT model, not
     this bundled default — the index tiers are derived from the stored vector,
     so the ladder isolates axis 1 on a fixed base model regardless of which
     model generated it. FP16/BF16 and NVFP4 rows require re-embedding and are
     recorded here as status stubs, not fabricated numbers.

Integrity: rather than a cross-language self-signature, each source artifact's
raw-bytes SHA-256 is recorded in `provenance`. Re-running this script on the
same artifacts reproduces the identical rows and hashes on any machine; a row
can be trusted exactly as far as its cited source file hashes match.

    python benchmark/embedding-quantization/aggregate.py            # -> report.json
    python benchmark/embedding-quantization/aggregate.py --out /tmp/q.json

Standalone dense recall is the honest quantization-quality signal (the hybrid
arm's keyword side nails the synthetic cluster markers and reads ~1.0 for every
tier — see #619). Both are carried; the recommendation matrix reads `dense`.
"""
import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
RESULTS = REPO / "benchmark" / "lambda" / "results"

# Index/signature-quantization tiers, each mapped to the signed 1M artifact that
# measured it. Order = increasing compression. `bits_per_dim` is exact from the
# scheme (dimension-independent); bytes/vec are quoted for the #619 corpus dim
# (nomic-embed-text, 768-dim -> f32 3072 B, int4 384 B, 1-bit 96 B).
TIERS = [
    {
        "level": "full_f32_exact_cosine",
        "column": "embedding",
        "bits_per_dim": 32,
        "compression": "1x",
        "source": "scale1m_exact_ceiling.json",
        "note": "unbounded exact cosine — the full-precision ceiling",
    },
    {
        "level": "int4_sig4_coarse",
        "column": "emb_sig4",
        "bits_per_dim": 4,
        "compression": "8x",
        "source": "scale1m_2c_on.json",
        "note": "resident int4 ADC signature coarse ranking (#619 step 2c')",
    },
    {
        "level": "1bit_sig_prefilter_rerank",
        "column": "emb_sig",
        "bits_per_dim": 1,
        "compression": "32x",
        "source": "scale1m_default_500.json",
        "shipped_default": True,
        "note": "1-bit Hamming prefilter + exact-cosine rerank — SHIPPED default",
    },
    {
        "level": "pure_1bit_hamming_only",
        "column": "emb_sig",
        "bits_per_dim": 1,
        "compression": "32x",
        "source": None,
        "status": "harness_ready_measurement_pending",
        "note": "Hamming-only, no rerank (MIMIR_DENSE_SIG_RERANK=0, #630). "
                "Upper-bounds what the exact rerank buys; measure on an "
                "embedded corpus (next Lambda run or a local model).",
    },
]

MODEL_AXIS = {
    "int8": {"status": "shipped",
             "note": "the bundled LOCAL default is all-MiniLM-L6-v2 qint8 "
                     "(384-dim, INT8) — used for the offline/default path and "
                     "the repo's 100K + LongMemEval numbers. NOTE: the 1M index "
                     "ladder above was measured on the nomic-768 ENDPOINT model, "
                     "not this bundled INT8 default (see corpus.embedding_model)"},
    "fp16_bf16": {"status": "measurement_pending",
                  "note": "re-embed with the full-precision ONNX export + rerun "
                          "the harness (cheap at 100K local)"},
    "nvfp4": {"status": "deferred",
              "note": "needs Blackwell-class hardware; the interesting question "
                      "(larger embedder @ NVFP4 vs MiniLM-FP32 at equal memory) "
                      "is tracked in #629"},
}

# The index-quantization tiers (f32/int4/1-bit) are DERIVED from whatever
# embedding is stored, so the ladder is model-agnostic in principle. The 1M runs
# fix the base model at nomic-768 across all three tiers, so the comparison is
# apples-to-apples; re-measuring the ladder on the bundled MiniLM-384 (or FP16)
# is the separate, pending model-weight axis.
INDEX_AXIS_NOTE = ("f32/int4/1-bit tiers are derived from the stored vector; the "
                   "1M ladder fixes the base model at nomic-768, so it isolates "
                   "the index axis apples-to-apples on that model. Whether the "
                   "denoising holds identically for the bundled MiniLM-384 is "
                   "plausible but unmeasured (the model-weight axis).")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pick(summary: dict, arm: str, k: int, field: str):
    """summary['recall@{k}']['uniform'][arm][field] — uniform = the honest set."""
    return summary["recall@%d" % k]["uniform"][arm][field]


def extract(summary: dict, arm: str) -> dict:
    row = {"p50_ms": pick(summary, arm, 1, "p50_ms"),
           "p99_ms": pick(summary, arm, 1, "p99_ms")}
    for k in (1, 5, 10):
        row["r@%d" % k] = pick(summary, arm, k, "recall")
    return row


def main():
    ap = argparse.ArgumentParser(description="Aggregate #630 quantization ladder")
    ap.add_argument("--out", default=str(HERE / "report.json"))
    args = ap.parse_args()

    provenance = {}
    rows = []
    for tier in TIERS:
        row = {k: tier[k] for k in tier if k != "source"}
        src = tier["source"]
        if src:
            p = RESULTS / src
            data = json.loads(p.read_text(encoding="utf-8"))
            summary = data["summary"]
            provenance[src] = {
                "sha256": sha256_file(p),
                "tier": data.get("tier"),
                "persisted": data.get("corpus", {}).get("persisted"),
                "max_scan": data.get("summary", {}).get("warm_set_info", {}).get("max_scan"),
            }
            row["source"] = src
            row["dense"] = extract(summary, "dense")
            row["hybrid"] = extract(summary, "hybrid")
        rows.append(row)

    report = {
        "benchmark": "embedding-quantization",
        "issue": "#630",
        "axes": {
            "index_signature": "compression of the stored vector for the dense "
                               "prefilter: full f32 cosine (1x) -> int4 emb_sig4 "
                               "(8x) -> 1-bit emb_sig (32x)",
            "model_weight": "generation of the embedding: INT8 (shipped MiniLM "
                            "qint8) -> FP16/BF16 -> NVFP4",
        },
        "corpus": {"scale": "1M", "shape": "10000 clusters x 100",
                   "embedding_model": "nomic-embed-text 768-dim (endpoint; H100 fleet)",
                   "hardware": "gpu_2x_h100_sxm5", "queries": 500},
        "honest_signal": "standalone dense recall (hybrid keyword arm saturates "
                         "~1.0 on the synthetic cluster markers)",
        "index_axis_note": INDEX_AXIS_NOTE,
        "index_ladder": rows,
        "model_axis": MODEL_AXIS,
        "provenance": provenance,
    }
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("wrote", args.out)
    for r in rows:
        d = r.get("dense")
        if d:
            print("  %-28s %-4s dense r@5=%.3f p50=%sms  [%s]" %
                  (r["level"], r["compression"], d["r@5"], d["p50_ms"], r["source"]))
        else:
            print("  %-28s %-4s %s" % (r["level"], r["compression"], r.get("status")))


if __name__ == "__main__":
    main()
