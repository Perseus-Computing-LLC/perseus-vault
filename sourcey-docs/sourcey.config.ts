import { defineConfig, rustdoc } from "sourcey";

export default defineConfig({
  name: "Perseus Vault",
  description: "Generated Rust API reference for Perseus Vault",
  navigation: {
    tabs: [
      {
        tab: "Rust API",
        slug: "rust-api",
        source: rustdoc({
          manifest: "../Cargo.toml",
          crates: ["perseus-vault"],
          mode: "live",
          features: { default: false },
          includePrivate: false,
          includeHidden: false,
          toolchain: "nightly",
          sourceBasePath: "",
          doctestsIndex: true
        })
      }
    ]
  }
});
