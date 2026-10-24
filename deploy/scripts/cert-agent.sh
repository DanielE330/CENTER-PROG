#!/bin/sh
# Issues this service's certificate from step-ca and keeps renewing it at 2/3 of
# its lifetime. Files land in a tmpfs owned by the service's uid.
set -eu
: "${NAME:?}" "${LIFETIME:=168h}"
export STEPPATH=/certs/.step
mkdir -p "$STEPPATH"
until [ -s /ca-public/root_ca.crt ]; do echo "waiting for CA"; sleep 1; done
cp /ca-public/root_ca.crt /certs/ca.pem

if [ ! -s /certs/cert.pem ]; then
  # EXTRA_SANS holds several "--san x" words on purpose.
  # shellcheck disable=SC2086
  until step ca certificate "$NAME" /certs/cert.pem /certs/key.pem \
      --ca-url https://step-ca:9000 --root /certs/ca.pem \
      --provisioner ci1 --provisioner-password-file /run/secrets/provisioner_password \
      --san "$NAME" --san "spiffe://ci1/svc/$NAME" ${EXTRA_SANS:-} \
      --kty EC --curve P-256 --not-after "$LIFETIME" --force; do
    echo "certificate request failed, retrying"; sleep 2
  done
  chmod 600 /certs/key.pem
  # Some runtimes want PKCS#8 (Keycloak, the JDBC driver); produce it next to the PEM.
  /bin/sh /agent/post-renew.sh
fi
echo "certificate ready for $NAME"
# Renewal authenticates with the current certificate; no password needed.
exec step ca renew --daemon --root /certs/ca.pem --ca-url https://step-ca:9000 \
  --exec "/bin/sh -c 'chmod 600 /certs/key.pem; PKCS8=${PKCS8:-false} /bin/sh /agent/post-renew.sh'" \
  /certs/cert.pem /certs/key.pem
