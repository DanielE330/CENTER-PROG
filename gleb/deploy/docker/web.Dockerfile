# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS build
RUN npm install -g pnpm@12.15.0 --no-fund --no-audit
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile
COPY web ./
RUN pnpm build

# Caddy is rebuilt with the current Go toolchain: the official binary lags behind Go security
# releases. A fresh build also has no file capabilities (we run with all capabilities dropped).
FROM golang:1.27.3-alpine AS caddy
RUN go install github.com/caddyserver/xcaddy/cmd/xcaddy@v0.4.7 \
 && CGO_ENABLED=0 xcaddy build v2.11.7 --output /caddy --replace golang.org/x/net=golang.org/x/net@v0.60.0

FROM caddy:2.11.7-alpine
# Alpine fixes land faster than new Caddy images.
RUN apk upgrade --no-cache
COPY --from=caddy /caddy /usr/bin/caddy
COPY --from=build /web/dist /srv
USER 1000:1000
