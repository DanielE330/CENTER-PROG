#!/bin/sh
# Runs `$@` (a reload command) whenever /certs/cert.pem changes. Used by servers
# that do not watch their certificate themselves (Postgres, NATS, Caddy).
set -eu
last=""
while true; do
  now=$(stat -c %Y /certs/cert.pem 2>/dev/null || echo "")
  if [ -n "$last" ] && [ "$now" != "$last" ]; then
    echo "certificate changed, reloading"; "$@" || echo "reload failed"
  fi
  last=$now
  sleep 30
done
