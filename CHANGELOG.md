# Changelog

All notable changes to Mimir are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Offline dense/hybrid search out of the box (#237).** A quantized
  all-MiniLM-L6-v2 model (int8, ~23 MB, 384-dim) is now fetched once by `build.rs`
  and **compiled into the binary**, and the embedding backend is **enabled by
  default**. Semantic recall works with zero config and zero network — no Ollama,
  no API key, no first-run model download — making the local-first / fully-offline
  promise literally true. Build a lean binary without the embedding stack via
  `cargo build --no-default-features`.

### Fixed
- **Native ONNX embedding now passes `token_type_ids`.** The `ort` inference path
  sent only `input_ids` + `attention_mask`; the (quantized) BERT graph requires
  the `token_type_ids` input (all-zeros for a single sequence), so native
  embedding failed at runtime. Now passed explicitly.

### CI
- The default build (now bundled-embeddings) is built **and tested** on Linux and
  **Windows MSVC** — including an end-to-end test that runs real inference through
  the compiled-in model — confirming the single-binary semantic-search claim on
  every platform. Added a `lite-build` job guarding `--no-default-features`.
