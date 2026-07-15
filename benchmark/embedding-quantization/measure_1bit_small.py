#!/usr/bin/env python3
"""Pure 1-bit (sign-quantized) recall benchmark for Perseus Vault.

Uses the bundled all-MiniLM-L6-v2 ONNX model to embed memories and queries,
then ranks purely by Hamming distance on the 1-bit sign signature — NO cosine
rerank. This is the "free offline row" from perseus-vault#630.

Compares against the full-precision cosine baseline from the standard
benchmark/recall/run.py dense mode.

Embedding pipeline matches the Rust binary exactly: encode with special tokens,
NO padding, mean pool over active tokens, L2 normalize.
"""

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

# ── configuration ──────────────────────────────────────────────────────────

MODEL_PATH = "/opt/data/webui/mimir-repo/target/release/build/perseus-vault-8a943c6d28eb020c/out/model_quantized.onnx"
TOKENIZER_PATH = "/opt/data/webui/mimir-repo/target/release/build/perseus-vault-8a943c6d28eb020c/out/tokenizer.json"
DATASET_PATH = "/opt/data/webui/mimir-repo/benchmark/recall/dataset.json"
BASELINE_PATH = "/tmp/baseline-recall.json"
OUT_PATH = "/tmp/1bit-recall.json"

# ── signature helpers (mirrors Rust embedding_signature + signature_hamming) ──

def embedding_signature(vec: np.ndarray) -> np.ndarray:
    """Pack sign bits: bit i is 1 if vec[i] > 0, else 0. Returns uint8 array."""
    bits = (vec > 0).astype(np.uint8)
    n_bytes = (len(vec) + 7) // 8
    sig = np.zeros(n_bytes, dtype=np.uint8)
    for i in range(len(vec)):
        if bits[i]:
            sig[i // 8] |= 1 << (i % 8)
    return sig

def hamming_distance(a: np.ndarray, b: np.ndarray) -> int:
    """Hamming distance between two uint8 signature arrays."""
    if len(a) != len(b):
        return 2**31 - 1
    return int(np.unpackbits(a ^ b).sum())

# ── embedding (EXACT MATCH to Rust generate_with_ort) ───────────────────────

def load_model_and_tokenizer():
    tok = Tokenizer.from_file(TOKENIZER_PATH)
    # Make sure padding is disabled (Rust path never pads single-sequence inputs)
    tok.no_padding()
    sess = ort.InferenceSession(
        MODEL_PATH,
        providers=["CPUExecutionProvider"],
        sess_options=_make_deterministic_options(),
    )
    return tok, sess

def _make_deterministic_options():
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    # Enable deterministic compute if available
    try:
        opts.add_session_config_entry("session.deterministic_compute", "1")
    except Exception:
        pass
    return opts

def embed_texts(tokenizer, session, texts: list[str]) -> np.ndarray:
    """Compute normalized mean-pooled embeddings matching Rust generate_with_ort.

    Key: NO padding — each input is passed at its exact tokenized length.
    This matches the Rust path which never pads single-sequence inputs.
    """
    embeddings = []
    for text in texts:
        # encode(text, add_special_tokens=True) — same as Rust tokenizer.encode(text, true)
        enc = tokenizer.encode(text)
        ids = np.array(enc.ids, dtype=np.int64)
        attn = np.array(enc.attention_mask, dtype=np.int64)
        type_ids = np.zeros(len(ids), dtype=np.int64)

        # batch=1 (add batch dimension)
        input_ids = ids[np.newaxis, :]
        attention_mask = attn[np.newaxis, :]
        token_type_ids = type_ids[np.newaxis, :]

        ort_inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        }
        outputs = session.run(None, ort_inputs)
        hidden = outputs[0]  # [1, seq_len, 384]

        # Mean pooling: average over tokens where attention_mask == 1
        # Matches Rust: for t in 0..seq_len { if attn[t]==1 { pool += row } } / active
        mask = attn.astype(np.float32)
        active = mask.sum()
        pooled = (hidden[0] * mask[:, np.newaxis]).sum(axis=0) / max(active, 1)

        # L2 normalize
        norm = np.linalg.norm(pooled)
        if norm > 0:
            pooled = pooled / norm

        embeddings.append(pooled)

    return np.array(embeddings, dtype=np.float32)

# ── recall scoring ──────────────────────────────────────────────────────────

def recall_at(ranked_keys: list, relevant: set, k: int) -> float:
    return 1.0 if relevant & set(ranked_keys[:k]) else 0.0

def reciprocal_rank(ranked_keys: list, relevant: set) -> float:
    for i, key in enumerate(ranked_keys):
        if key in relevant:
            return 1.0 / (i + 1)
    return 0.0

# ── main ────────────────────────────────────────────────────────────────────

def main():
    print("Loading model & tokenizer...", flush=True)
    tok, sess = load_model_and_tokenizer()
    print(f"  ONNX providers: {sess.get_providers()}", flush=True)

    # Load dataset
    data = json.loads(Path(DATASET_PATH).read_text())
    memories = data["memories"]
    queries = data["queries"]
    ks = [1, 3, 5]

    # Embed memories
    t0 = time.monotonic()
    mem_texts = [m["note"] for m in memories]
    mem_keys = [m["key"] for m in memories]
    print(f"\nEmbedding {len(mem_texts)} memories...", flush=True)
    mem_embs = embed_texts(tok, sess, mem_texts)
    print(f"  Done in {time.monotonic()-t0:.1f}s, shape={mem_embs.shape}", flush=True)

    # Embed queries
    query_texts = [q["q"] for q in queries]
    print(f"Embedding {len(query_texts)} queries...", flush=True)
    query_embs = embed_texts(tok, sess, query_texts)

    # Compute signatures (1-bit)
    mem_sigs = np.array([embedding_signature(e) for e in mem_embs])
    query_sigs = np.array([embedding_signature(e) for e in query_embs])
    print(f"  Signatures: {mem_sigs.shape[1]} bytes each ({mem_embs.shape[1]} dims)", flush=True)

    # ── 1-bit (Hamming-only) scoring ──
    print("\n── Pure 1-bit (Hamming-only) ──")
    agg_1bit = {f"recall@{k}": 0.0 for k in ks}
    agg_1bit["mrr"] = 0.0
    per_query_1bit = []

    t1 = time.monotonic()
    for qi, q_sig in enumerate(query_sigs):
        dists = [hamming_distance(q_sig, ms) for ms in mem_sigs]
        ranked_indices = sorted(range(len(dists)), key=lambda i: (dists[i], i))
        ranked_keys = [mem_keys[i] for i in ranked_indices]
        relevant = set(queries[qi]["relevant"])

        row = {"q": queries[qi]["q"], "relevant": list(relevant), "ranked": ranked_keys[:max(ks)]}
        for k in ks:
            r = recall_at(ranked_keys, relevant, k)
            agg_1bit[f"recall@{k}"] += r
            row[f"recall@{k}"] = r
        rr = reciprocal_rank(ranked_keys, relevant)
        agg_1bit["mrr"] += rr
        row["rr"] = rr
        per_query_1bit.append(row)

    t_elapsed = time.monotonic() - t1
    n = len(queries)
    for key in agg_1bit:
        agg_1bit[key] = round(agg_1bit[key] / n, 4)

    print(f"  recall@1: {agg_1bit['recall@1']*100:.1f}%")
    print(f"  recall@3: {agg_1bit['recall@3']*100:.1f}%")
    print(f"  recall@5: {agg_1bit['recall@5']*100:.1f}%")
    print(f"  MRR:      {agg_1bit['mrr']:.3f}")
    print(f"  Scoring time: {t_elapsed*1000:.1f}ms ({t_elapsed/n*1000:.2f}ms/query)")

    # ── Full-precision (cosine) scoring ──
    print("\n── Full-precision (cosine) ──")
    agg_full = {f"recall@{k}": 0.0 for k in ks}
    agg_full["mrr"] = 0.0
    per_query_full = []

    t2 = time.monotonic()
    for qi, q_emb in enumerate(query_embs):
        sims = np.dot(mem_embs, q_emb)
        ranked_indices = np.argsort(-sims)
        ranked_keys = [mem_keys[i] for i in ranked_indices]
        relevant = set(queries[qi]["relevant"])

        row = {"q": queries[qi]["q"], "relevant": list(relevant), "ranked": ranked_keys[:max(ks)]}
        for k in ks:
            r = recall_at(ranked_keys, relevant, k)
            agg_full[f"recall@{k}"] += r
            row[f"recall@{k}"] = r
        rr = reciprocal_rank(ranked_keys, relevant)
        agg_full["mrr"] += rr
        row["rr"] = rr
        per_query_full.append(row)

    t_elapsed2 = time.monotonic() - t2
    for key in agg_full:
        agg_full[key] = round(agg_full[key] / n, 4)

    print(f"  recall@1: {agg_full['recall@1']*100:.1f}%")
    print(f"  recall@3: {agg_full['recall@3']*100:.1f}%")
    print(f"  recall@5: {agg_full['recall@5']*100:.1f}%")
    print(f"  MRR:      {agg_full['mrr']:.3f}")
    print(f"  Scoring time: {t_elapsed2*1000:.1f}ms ({t_elapsed2/n*1000:.2f}ms/query)")

    # ── Load binary baseline for comparison ──
    print("\n── Comparison ──")
    try:
        baseline = json.loads(Path(BASELINE_PATH).read_text())
        bm = baseline["metrics"]["dense"]
        print(f"  {'':>20}  {'Binary dense':>12}  {'1-bit pure':>12}  {'Full FP (ours)':>16}")
        print(f"  {'─'*62}")
        for k in ks:
            bv = bm[f"recall@{k}"]*100
            ov = agg_1bit[f"recall@{k}"]*100
            fv = agg_full[f"recall@{k}"]*100
            print(f"  recall@{k:<16}  {bv:>11.1f}%  {ov:>11.1f}%  {fv:>15.1f}%")
        b_mrr = bm["mrr"]
        o_mrr = agg_1bit["mrr"]
        f_mrr = agg_full["mrr"]
        print(f"  {'MRR':>20}  {b_mrr:>12.3f}  {o_mrr:>11.3f}  {f_mrr:>15.3f}")
    except FileNotFoundError:
        print(f"  (no binary baseline found at {BASELINE_PATH})")

    # ── Write output ──
    report = {
        "benchmark": "perseus-vault-1bit-recall",
        "dataset": data.get("name"),
        "n_memories": len(memories),
        "n_queries": n,
        "k": ks,
        "embedding": {
            "model": "all-MiniLM-L6-v2 (quantized ONNX)",
            "dimensions": int(mem_embs.shape[1]),
            "signature_bytes": int(mem_sigs.shape[1]),
        },
        "1bit": {"metrics": agg_1bit, "per_query": per_query_1bit},
        "full_precision": {"metrics": agg_full, "per_query": per_query_full},
    }
    Path(OUT_PATH).write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nReport: {OUT_PATH}")

    # ── Quality ratio for Plutus cost model ──
    print("\n── Plutus multiplier guidance ──")
    if agg_full["recall@1"] > 0:
        r1_ratio = agg_1bit["recall@1"] / agg_full["recall@1"]
        print(f"  1-bit / full-precision recall@1 ratio: {r1_ratio:.3f}")
    if agg_full["mrr"] > 0:
        mrr_ratio = agg_1bit["mrr"] / agg_full["mrr"]
        print(f"  1-bit / full-precision MRR ratio: {mrr_ratio:.3f}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
