#!/bin/sh
set -eu
if [ "${PKCS8:-false}" = "true" ]; then
  step crypto key format --pkcs8 --pem --no-password --insecure --force --out /certs/key-pkcs8.pem /certs/key.pem
  step crypto key format --pkcs8 --der --no-password --insecure --force --out /certs/key.pk8 /certs/key.pem
fi
echo "certificate renewed"
