# syntax=docker/dockerfile:1
# Build context: repo root. SPA собирается здесь и раздаётся Caddy.
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM caddy:2-alpine
# порт 3000 непривилегированный: снимаем file capability, иначе exec падает под cap_drop ALL + no-new-privileges
RUN apk add --no-cache libcap && setcap -r /usr/bin/caddy && apk del libcap
COPY infra/docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=frontend /frontend/dist /srv
EXPOSE 3000
