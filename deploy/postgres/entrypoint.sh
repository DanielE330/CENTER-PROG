#!/bin/bash
# Starts Postgres with TLS from the cert-agent and reloads it after renewal.
set -euo pipefail
/agent/reload-on-renew.sh pg_ctl reload -D "$PGDATA" &
exec docker-entrypoint.sh postgres \
  -c ssl=on -c ssl_cert_file=/certs/cert.pem -c ssl_key_file=/certs/key.pem -c ssl_ca_file=/certs/ca.pem \
  -c ssl_min_protocol_version=TLSv1.2 -c hba_file=/etc/postgresql/pg_hba.conf \
  -c password_encryption=scram-sha-256 -c log_connections=on -c shared_buffers=64MB
