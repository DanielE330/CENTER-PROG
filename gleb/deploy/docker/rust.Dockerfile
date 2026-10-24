# syntax=docker/dockerfile:1.7
# All Rust services are built once in the same builder stage; each image copies its binary.
FROM rust:1.98-bookworm AS builder
WORKDIR /src
COPY Cargo.toml Cargo.lock rustfmt.toml clippy.toml ./
COPY crates crates
COPY services/gateway services/gateway
COPY services/tenancy services/tenancy
COPY services/ingest services/ingest
COPY services/obligations services/obligations
COPY services/alerts services/alerts
COPY services/llm-gateway services/llm-gateway
COPY services/advisor-tools services/advisor-tools
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/src/target \
    cargo build --release --locked --workspace \
 && mkdir /out \
 && for b in gateway tenancy ingest obligations alerts llm-gateway advisor-tools; do cp "target/release/$b" /out/; done

FROM gcr.io/distroless/cc-debian12:nonroot
ARG SERVICE
COPY --from=builder /out/${SERVICE} /app/service
USER 65532:65532
ENTRYPOINT ["/app/service"]
