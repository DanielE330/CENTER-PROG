#!/usr/bin/env bash
# Загружает демо-данные в демо-пользователя: выписки компаний из датасета (по умолчанию C001: смешивать выписки разных компаний в одном аккаунте нельзя, остатки не сойдутся),
# подтверждает импорт и строит прогноз. Запуск: infra/scripts/seed_demo.sh [BASE_URL] [C001 C004 ...]
# Демо «Прошла неделя»: демо-пользователь получает выписку только ДО даты отсечки DEMO_CUTOFF (по умолчанию 2026-07-12),
# остальное отдаёт сервис demo_feed по кнопке в интерфейсе. DEMO_CUTOFF=none — загрузить выписку целиком.
# Берёт data/dataset/statements/, а если его нет — демо-выписку data/demo/statements/ из репозитория (там только C001). Демо-вход: demo@example.com / demo12345 (DEMO_SEED=1).
set -euo pipefail
cd "$(dirname "$0")/../.."
BASE=${1:-http://localhost:3000}; shift || true
COMPANIES=${*:-C001}
CUTOFF=${DEMO_CUTOFF:-2026-07-12}
JAR=$(mktemp); TMP=$(mktemp -d); trap 'rm -rf "$JAR" "$TMP"' EXIT
# строки выписки с operation_date <= дата отсечки; заголовок и формат файла не меняются
cut_statement() {
  python3 - "$1" "$2" "$CUTOFF" <<'PY'
import csv, sys
src, dst, cutoff = sys.argv[1:4]
with open(src, encoding="utf-8-sig", newline="") as f, open(dst, "w", encoding="utf-8-sig", newline="") as o:
    rd, w = csv.reader(f, delimiter=";"), csv.writer(o, delimiter=";", lineterminator="\n")
    head = next(rd); w.writerow(head); col = head.index("operation_date"); n = 0
    for r in rd:
        if r and r[col] <= cutoff:
            w.writerow(r); n += 1
print(f"{n} операций до {cutoff}", file=sys.stderr)
PY
}
ME=$(curl -fsS -c "$JAR" -X POST "$BASE/api/v1/auth/login" -H 'content-type: application/json' \
  -d '{"email":"demo@example.com","password":"demo12345"}')
CID=$(printf '%s' "$ME" | python3 -c 'import json,sys; print(json.load(sys.stdin)["company_id"])')
jq_id() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("import_id",""))'; }
for C in $COMPANIES; do
  DIR=data/dataset; [ -d "$DIR/statements" ] || DIR=data/demo
  F=$DIR/statements/$C.csv
  [ -f "$F" ] || { echo "нет файла $F"; exit 1; }
  if [ "$CUTOFF" != none ]; then cut_statement "$F" "$TMP/$C.csv" && F=$TMP/$C.csv; fi
  RESP=$(curl -sS -b "$JAR" -F "file=@$F" "$BASE/api/v1/companies/$CID/imports")
  IID=$(printf '%s' "$RESP" | jq_id)
  if [ -z "$IID" ]; then echo "$C: пропущено ($RESP)"; continue; fi
  curl -fsS -b "$JAR" -X POST "$BASE/api/v1/companies/$CID/imports/$IID/confirm" >/dev/null
  echo "$C: импортировано"
done
curl -fsS -b "$JAR" -X POST "$BASE/api/v1/companies/$CID/forecasts" -H 'content-type: application/json' \
  -d '{"horizon_days":30}' >/dev/null && echo "прогноз построен для демо-пользователя"
