#!/bin/bash
# Keycloak with TLS from the cert-agent, Postgres over mTLS (no DB password) and
# the realm imported from code. Secrets come from files, not environment.
set -euo pipefail
KC_BOOTSTRAP_ADMIN_PASSWORD="$(cat /run/secrets/keycloak_admin_password)"
GATEWAY_CLIENT_SECRET="$(cat /run/secrets/gateway_client_secret)"
export KC_BOOTSTRAP_ADMIN_PASSWORD GATEWAY_CLIENT_SECRET
until [ -s /certs/key.pk8 ]; do echo "waiting for certificate"; sleep 1; done
mkdir -p /opt/keycloak/data/import
cp /realm/ci1-realm.json /opt/keycloak/data/import/ci1-realm.json
exec /opt/keycloak/bin/kc.sh start --import-realm
