# Glama-compatible Dockerfile for Mimir
# Builds a static musl binary for Firecracker microVM sandbox execution
FROM rust:1.96-alpine AS builder
RUN apk add --no-cache musl-dev sqlite-dev
WORKDIR /app
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/
RUN cargo build --release && strip target/release/mimir

FROM alpine:3.21
RUN apk add --no-cache sqlite-libs
COPY --from=builder /app/target/release/mimir /usr/local/bin/mimir
ENTRYPOINT ["/usr/local/bin/mimir"]
CMD ["--db", "/data/mimir.db"]
