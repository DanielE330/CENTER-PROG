# syntax=docker/dockerfile:1.7
FROM node:22-bookworm-slim AS build
RUN npm install -g pnpm@12.15.0 --no-fund --no-audit
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile
COPY web ./
RUN pnpm build

FROM caddy:2.10-alpine
# Caddy ships with cap_net_bind_service as a file capability; we listen on 8443 and run with
# all capabilities dropped and no-new-privileges, under which such a binary cannot be executed.
# A plain copy does not carry the security.capability xattr.
RUN cp /usr/bin/caddy /tmp/caddy && mv /tmp/caddy /usr/bin/caddy && chmod 755 /usr/bin/caddy
COPY --from=build /web/dist /srv
