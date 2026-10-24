#!/bin/sh
# NATS with mutual TLS; the user is mapped from the client certificate.
set -eu
until [ -s /certs/cert.pem ]; do echo "waiting for certificate"; sleep 1; done
/agent/reload-on-renew.sh nats-server --signal reload &
exec nats-server -c /etc/nats/nats.conf
