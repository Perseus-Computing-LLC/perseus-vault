use std::path::Path;

fn main() {
    #[cfg(feature = "grpc")]
    {
        tonic_build::configure()
            .build_server(true)
            .build_client(false)
            .compile_protos(&["proto/mimir/v1/mimir.proto"], &["proto"])
            .expect("failed to compile mimir proto");
    }

    // #237: when bundled-embeddings is active, fetch the quantized
    // all-MiniLM-L6-v2 model + tokenizer once into OUT_DIR so embedding.rs can
    // `include_bytes!` them into the binary. The result is a single self-contained
    // binary that does dense/hybrid search with zero network at runtime. Cached in
    // OUT_DIR across incremental builds; a clean build downloads ~23MB once.
    if std::env::var("CARGO_FEATURE_BUNDLED_EMBEDDINGS").is_ok() {
        let out_dir = std::env::var("OUT_DIR").expect("OUT_DIR not set");
        fetch_model_assets(&out_dir);
    }

    println!("cargo:rerun-if-changed=build.rs");
}

#[allow(dead_code)]
fn fetch_model_assets(out_dir: &str) {
    // The int8 dynamic-quantized ONNX export (~23MB vs ~90MB fp32; 384-dim, recall
    // within noise) plus its tokenizer, from the SAME sentence-transformers repo as
    // the fp32 model — so it keeps the (input_ids, attention_mask) -> last_hidden_state
    // signature the inference code already handles. The qint8 ops run on any CPU via
    // ONNX Runtime's CPU EP (the arch suffix is just the export's calibration preset).
    const MODEL_URL: &str = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_avx512_vnni.onnx";
    const TOKENIZER_URL: &str = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json";
    // Allow an operator/CI to pre-place or override the model dir (offline builds,
    // air-gapped CI) instead of downloading.
    if let Ok(dir) = std::env::var("MIMIR_BUNDLED_MODEL_DIR") {
        copy_if_present(&dir, "model_quantized.onnx", out_dir);
        copy_if_present(&dir, "tokenizer.json", out_dir);
    }
    download_to(MODEL_URL, &format!("{out_dir}/model_quantized.onnx"), 10_000_000);
    download_to(TOKENIZER_URL, &format!("{out_dir}/tokenizer.json"), 100_000);
}

#[allow(dead_code)]
fn copy_if_present(src_dir: &str, name: &str, out_dir: &str) {
    let src = Path::new(src_dir).join(name);
    if src.exists() {
        let _ = std::fs::copy(&src, Path::new(out_dir).join(name));
    }
}

#[allow(dead_code)]
fn download_to(url: &str, dest: &str, min_bytes: u64) {
    // Skip if a valid (non-truncated) file is already cached in OUT_DIR.
    if let Ok(meta) = std::fs::metadata(dest) {
        if meta.len() >= min_bytes {
            return;
        }
    }
    let resp = ureq::get(url)
        .timeout(std::time::Duration::from_secs(600))
        .call()
        .unwrap_or_else(|e| panic!("build.rs: failed to download {url}: {e}\nFor an offline build, set MIMIR_BUNDLED_MODEL_DIR to a dir containing the model + tokenizer, or build with --no-default-features."));
    let mut reader = resp.into_reader();
    let tmp = format!("{dest}.tmp");
    let mut file = std::fs::File::create(&tmp)
        .unwrap_or_else(|e| panic!("build.rs: cannot create {tmp}: {e}"));
    let n = std::io::copy(&mut reader, &mut file)
        .unwrap_or_else(|e| panic!("build.rs: download write failed for {url}: {e}"));
    assert!(
        n >= min_bytes,
        "build.rs: downloaded {url} is only {n} bytes (< {min_bytes}); likely truncated/blocked"
    );
    std::fs::rename(&tmp, dest)
        .unwrap_or_else(|e| panic!("build.rs: cannot finalize {dest}: {e}"));
}
