# syntax=docker/dockerfile:1
# Build context: repo root.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM rust:1-bookworm AS build
WORKDIR /src
COPY backend/api/rust-toolchain.toml backend/api/Cargo.toml backend/api/Cargo.lock ./
# dependency cache layer
RUN mkdir src && echo 'fn main() {}' > src/main.rs \
    && cargo build --release --locked && rm -rf src target/release/api target/release/deps/api-*
COPY backend/api/src ./src
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app /data
COPY --from=build /src/target/release/api /usr/local/bin/api
COPY --from=web /web/dist /app/web/dist
ENV PUBLIC_ADDR=0.0.0.0:3000 INTERNAL_ADDR=0.0.0.0:3100 \
    DB_PATH=/data/core.db WEB_DIR=/app/web/dist
VOLUME /data
USER app
EXPOSE 3000
CMD ["api"]
