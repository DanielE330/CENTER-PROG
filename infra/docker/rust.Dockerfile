# syntax=docker/dockerfile:1
# Build context: repo root. Targets (по образу на сервис): ledger, forecast, planfact, api.
FROM rust:1-bookworm AS build
WORKDIR /src
COPY backend/rust-toolchain.toml backend/Cargo.toml backend/Cargo.lock ./
COPY backend/common ./common
COPY backend/api ./api
COPY backend/ledger ./ledger
COPY backend/forecast ./forecast
COPY backend/planfact ./planfact
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/src/target \
    cargo build --release --locked --workspace --bins \
    && mkdir /out && cp target/release/api target/release/ledger target/release/forecast target/release/planfact /out/

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app /data
VOLUME /data
USER app

FROM runtime AS ledger
COPY --from=build /out/ledger /usr/local/bin/ledger
ENV INTERNAL_ADDR=0.0.0.0:3002 DB_PATH=/data/ledger.db
EXPOSE 3002
CMD ["ledger"]

FROM runtime AS forecast
COPY --from=build /out/forecast /usr/local/bin/forecast
ENV INTERNAL_ADDR=0.0.0.0:3003 DB_PATH=/data/forecast.db
EXPOSE 3003
CMD ["forecast"]

FROM runtime AS planfact
COPY --from=build /out/planfact /usr/local/bin/planfact
ENV INTERNAL_ADDR=0.0.0.0:3004
EXPOSE 3004
CMD ["planfact"]

# api: публичный BFF; SPA собирается отдельным этапом и раздаётся им при запуске без gateway
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM runtime AS api
COPY --from=build /out/api /usr/local/bin/api
COPY --from=frontend /frontend/dist /app/frontend/dist
ENV PUBLIC_ADDR=0.0.0.0:3000 INTERNAL_ADDR=0.0.0.0:3100 WEB_DIR=/app/frontend/dist
EXPOSE 3000
CMD ["api"]
