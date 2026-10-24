#!/usr/bin/env bash
# Интеграционный smoke-тест через публичный API (curl): регистрация, загрузка выписки, прогноз,
# пересчёт после нового события, налоговый календарь, план-факт, синхронизация интеграций (если integrator доступен).
# Запуск: infra/scripts/smoke.sh [BASE_URL]   (по умолчанию http://localhost:3000; сервисы поднимает infra/scripts/dev.sh)
set -uo pipefail
cd "$(dirname "$0")/../.."
BASE=${1:-http://localhost:3000}
JAR=$(mktemp); trap 'rm -f "$JAR" /tmp/smoke_body.$$' EXIT
EMAIL="smoke$(date +%s%N)@example.com"
FAIL=0
ok()   { echo "ok   $1"; }
bad()  { echo "FAIL $1"; FAIL=1; }
# req METHOD PATH [json-body] -> тело в $BODY, код в $CODE
req() {
  local m=$1 p=$2 d=${3:-}
  if [ -n "$d" ]; then
    CODE=$(curl -s -o /tmp/smoke_body.$$ -w '%{http_code}' -X "$m" -b "$JAR" -c "$JAR" -H "Origin: $BASE" -H 'Content-Type: application/json' -d "$d" "$BASE$p")
  else
    CODE=$(curl -s -o /tmp/smoke_body.$$ -w '%{http_code}' -X "$m" -b "$JAR" -c "$JAR" -H "Origin: $BASE" "$BASE$p")
  fi
  BODY=$(cat /tmp/smoke_body.$$)
}
jq_() { python3 -c 'import sys,json; d=json.load(sys.stdin)
for k in sys.argv[1].split("."): d=d[int(k)] if k.isdigit() else d[k]
print(d if not isinstance(d,bool) else str(d).lower())' "$1"; }
expect() { [ "$CODE" = "$2" ] && ok "$1 ($CODE)" || { bad "$1: ожидали $2, получили $CODE: ${BODY:0:300}"; }; }

curl -fsS "$BASE/live" >/dev/null && ok "live" || { bad "live"; exit 1; }

req POST /api/v1/auth/register "{\"email\":\"$EMAIL\",\"password\":\"smoke-pass-123\",\"password_confirm\":\"smoke-pass-123\",\"company_name\":\"Smoke LLC\"}"
expect "register" 200
CID=$(echo "$BODY" | jq_ company_id)
req GET /api/v1/auth/me; expect "me" 200
req GET "/api/v1/companies/$CID/forecasts/latest"; expect "прогноза ещё нет" 404

# CSRF и чужая компания
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST -b "$JAR" -H 'Origin: http://evil.example' "$BASE/api/v1/auth/logout"); [ "$CODE" = 403 ] && ok "CSRF: чужой origin 403" || bad "CSRF: $CODE"
req GET /api/v1/companies/00000000-0000-0000-0000-000000000000/transactions; expect "чужая компания" 404

# импорт
CODE=$(curl -s -o /tmp/smoke_body.$$ -w '%{http_code}' -b "$JAR" -H "Origin: $BASE" -F "file=@data/dataset/statements/C001.csv" "$BASE/api/v1/companies/$CID/imports"); BODY=$(cat /tmp/smoke_body.$$)
expect "загрузка выписки" 201
IID=$(echo "$BODY" | jq_ import_id)
req POST "/api/v1/companies/$CID/imports/$IID/confirm"; expect "подтверждение импорта" 200
req GET "/api/v1/companies/$CID/transactions?limit=5"; expect "операции" 200
TOTAL=$(echo "$BODY" | jq_ total); [ "$TOTAL" -gt 0 ] && ok "операций: $TOTAL" || bad "операций нет"

# прогноз
req POST "/api/v1/companies/$CID/forecasts" '{"horizon_days":30}'; expect "прогноз" 201
req GET "/api/v1/companies/$CID/forecasts/latest"; expect "latest" 200
[ "$(echo "$BODY" | jq_ stale)" = false ] && ok "stale=false" || bad "stale должен быть false"
[ "$(echo "$BODY" | jq_ recomputed)" = false ] && ok "recomputed=false" || bad "recomputed должен быть false"

# новое событие -> пересчёт при чтении latest
D=$(date -d '+20 days' +%F)
req POST "/api/v1/companies/$CID/expected-events" "{\"date\":\"$D\",\"amount_kop\":\"5000000\",\"direction\":\"out\",\"title\":\"Smoke expense\",\"source\":\"manual\"}"
expect "добавление события" 201
req GET "/api/v1/companies/$CID/forecasts/latest"; expect "latest после события" 200
[ "$(echo "$BODY" | jq_ recomputed)" = true ] && ok "recomputed=true" || bad "recomputed должен быть true"
[ "$(echo "$BODY" | jq_ stale)" = false ] && ok "stale=false после пересчёта" || bad "stale"

req POST "/api/v1/companies/$CID/scenarios" '{"horizon_days":30,"scenario":{"delay_days":7}}'; expect "сценарий" 201
req GET "/api/v1/companies/$CID/backtest"; [ "$CODE" = 200 ] || [ "$CODE" = 422 ] && ok "backtest ($CODE)" || bad "backtest $CODE"
req GET "/api/v1/companies/$CID/plan-fact"; expect "план-факт" 200
req GET "/api/v1/companies/$CID/expected-events"; expect "список событий" 200

# налоговый календарь (gov может быть недоступен: тогда 28-е число)
req POST "/api/v1/companies/$CID/expected-events/tax-calendar" '{}'
case "$CODE" in 201) ok "налоговый календарь: $(echo "$BODY" | jq_ source)";; 422) ok "налоговый календарь: нет налоговых платежей в выписке (422)";; *) bad "налоговый календарь $CODE: ${BODY:0:200}";; esac

# синхронизация интеграций: только если integrator отвечает
if curl -fsS -m 3 "${INTEGRATOR_URL:-http://127.0.0.1:8008}/live" >/dev/null 2>&1; then
  req POST "/api/v1/companies/$CID/integrations/sync" '{"external_company":"C001"}'; expect "sync интеграций" 200
  C1=$(echo "$BODY" | jq_ created)
  req POST "/api/v1/companies/$CID/integrations/sync" '{"external_company":"C001"}'; expect "sync повторно" 200
  [ "$(echo "$BODY" | jq_ created)" = 0 ] && ok "идемпотентность: created=0 (в первый раз $C1)" || bad "повторный sync создал события"
else
  echo "skip sync интеграций: integrator недоступен"
fi

req POST /api/v1/auth/logout; expect "logout" 204
req GET /api/v1/auth/me; expect "me после выхода" 401
[ $FAIL = 0 ] && echo "SMOKE PASSED" || { echo "SMOKE FAILED"; exit 1; }
