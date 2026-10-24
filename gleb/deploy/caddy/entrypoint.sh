#!/bin/sh
set -eu
until [ -s /certs/cert.pem ]; do echo "waiting for certificate"; sleep 1; done
/agent/reload-on-renew.sh caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile &
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
