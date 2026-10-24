#!/bin/sh
# Initializes the internal CA once, raises the certificate lifetime limit for the
# demo, publishes only the root certificate to the ca-public volume.
set -eu
export STEPPATH=/home/step
if [ ! -f "$STEPPATH/config/ca.json" ]; then
  step ca init --name "ci1 internal" --dns step-ca --address :9000 \
    --provisioner ci1 --password-file /run/secrets/provisioner_password \
    --provisioner-password-file /run/secrets/provisioner_password --deployment-type standalone
  step ca provisioner update ci1 --x509-min-dur=5m --x509-default-dur=24h --x509-max-dur="${MAX_LIFETIME:-168h}" \
    --ca-config "$STEPPATH/config/ca.json"
fi
cp "$STEPPATH/certs/root_ca.crt" /ca-public/root_ca.crt
chmod 644 /ca-public/root_ca.crt
exec step-ca --password-file /run/secrets/provisioner_password "$STEPPATH/config/ca.json"
