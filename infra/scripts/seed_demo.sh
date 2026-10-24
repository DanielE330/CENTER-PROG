#!/usr/bin/env bash
# Загружает демо-данные в демо-пользователя: выписки компаний из датасета (по умолчанию C001: смешивать выписки разных компаний в одном аккаунте нельзя, остатки не сойдутся),
# подтверждает импорт и строит прогноз. Запуск: infra/scripts/seed_demo.sh [BASE_URL] [C001 C004 ...]
# Берёт data/dataset/statements/, а если его нет — демо-выписку data/demo/statements/ из репозитория (там только C001). Демо-вход: demo@example.com / demo12345 (DEMO_SEED=1).
set -euo pipefail
cd "$(dirname "$0")/../.."
BASE=${1:-http://localhost:3000}; shift || true
COMPANIES=${*:-C001}
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
ME=$(curl -fsS -c "$JAR" -X POST "$BASE/api/v1/auth/login" -H 'content-type: application/json' \
  -d '{"email":"demo@example.com","password":"demo12345"}')
CID=$(printf '%s' "$ME" | python3 -c 'import json,sys; print(json.load(sys.stdin)["company_id"])')
jq_id() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("import_id",""))'; }
for C in $COMPANIES; do
  DIR=data/dataset; [ -d "$DIR/statements" ] || DIR=data/demo
  F=$DIR/statements/$C.csv
  [ -f "$F" ] || { echo "нет файла $F"; exit 1; }
  RESP=$(curl -sS -b "$JAR" -F "file=@$F" "$BASE/api/v1/companies/$CID/imports")
  IID=$(printf '%s' "$RESP" | jq_id)
  if [ -z "$IID" ]; then echo "$C: пропущено ($RESP)"; continue; fi
  curl -fsS -b "$JAR" -X POST "$BASE/api/v1/companies/$CID/imports/$IID/confirm" >/dev/null
  echo "$C: импортировано"
done
curl -fsS -b "$JAR" -X POST "$BASE/api/v1/companies/$CID/forecasts" -H 'content-type: application/json' \
  -d '{"horizon_days":30}' >/dev/null && echo "прогноз построен для демо-пользователя"
