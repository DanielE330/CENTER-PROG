# syntax=docker/dockerfile:1
# Build context: repo root. SPA собирается здесь и раздаётся Caddy.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM caddy:2-alpine
COPY infra/docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=web /web/dist /srv
EXPOSE 3000
