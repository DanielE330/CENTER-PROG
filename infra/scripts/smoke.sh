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
# полный датасет, если скачан, иначе демо-выписка из репозитория
STMT=data/dataset/statements/C001.csv; [ -f "$STMT" ] || STMT=data/demo/statements/C001.csv
CODE=$(curl -s -o /tmp/smoke_body.$$ -w '%{http_code}' -b "$JAR" -H "Origin: $BASE" -F "file=@$STMT" "$BASE/api/v1/companies/$CID/imports"); BODY=$(cat /tmp/smoke_body.$$)
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

# сведения ФНС: налоги на год, ЕНС, контрагенты (только если fns отвечает; нужен ИНН компании в реквизитах)
if curl -fsS -m 3 "${FNS_URL:-http://127.0.0.1:8015}/live" >/dev/null 2>&1; then
  req POST "/api/v1/companies/$CID/integrations/fns/sync" '{}'
  case "$CODE" in 200) ok "ФНС: налоги на год (создано $(echo "$BODY" | jq_ created), источник $(echo "$BODY" | jq_ source))";; 422) ok "ФНС: у компании нет ИНН (422 inn_required)";; *) bad "ФНС sync $CODE: ${BODY:0:200}";; esac
  req GET "/api/v1/companies/$CID/fns/counterparties?top=5"
  case "$CODE" in 200|422) ok "ФНС: контрагенты ($CODE)";; *) bad "ФНС контрагенты $CODE: ${BODY:0:200}";; esac
  req GET "/api/v1/companies/$CID/fns/ens"
  case "$CODE" in 200|404|422) ok "ФНС: ЕНС ($CODE)";; *) bad "ФНС ЕНС $CODE: ${BODY:0:200}";; esac
else
  echo "skip ФНС: сервис fns недоступен"
fi

# синхронизация интеграций: только если integrator отвечает
if curl -fsS -m 3 "${INTEGRATOR_URL:-http://127.0.0.1:8008}/live" >/dev/null 2>&1; then
  req POST "/api/v1/companies/$CID/integrations/sync" '{"external_company":"C001"}'; expect "sync интеграций" 200
  C1=$(echo "$BODY" | jq_ created)
  req POST "/api/v1/companies/$CID/integrations/sync" '{"external_company":"C001"}'; expect "sync повторно" 200
  [ "$(echo "$BODY" | jq_ created)" = 0 ] && ok "идемпотентность: created=0 (в первый раз $C1)" || bad "повторный sync создал события"
else
  echo "skip sync интеграций: integrator недоступен"
fi

# демо «Прошла неделя» (необязательно): только если лента демо отвечает и demo@example.com засеян выпиской до даты отсечки
# (infra/scripts/seed_demo.sh на чистой БД). Отдельная сессия: у demo-пользователя своя компания.
if curl -fsS -m 3 "${DEMO_FEED_URL:-http://127.0.0.1:8012}/live" >/dev/null 2>&1; then
  DJAR=$(mktemp); DCID=$(curl -s -c "$DJAR" -H 'Content-Type: application/json' -d '{"email":"demo@example.com","password":"demo12345"}' "$BASE/api/v1/auth/login" | jq_ company_id 2>/dev/null || true)
  if [ -n "$DCID" ]; then
    DEMO_ST=$(curl -s -b "$DJAR" "$BASE/api/v1/companies/$DCID/demo/status")
    if [ "$(echo "$DEMO_ST" | jq_ enabled 2>/dev/null)" = true ] && [ "$(echo "$DEMO_ST" | jq_ finished 2>/dev/null)" = false ]; then
      BEFORE=$(echo "$DEMO_ST" | jq_ as_of)
      DCODE=$(curl -s -o /tmp/smoke_body.$$ -w '%{http_code}' -X POST -b "$DJAR" -H "Origin: $BASE" "$BASE/api/v1/companies/$DCID/demo/advance-week"); BODY=$(cat /tmp/smoke_body.$$)
      if [ "$DCODE" = 201 ] && [ "$(echo "$BODY" | jq_ added)" -gt 0 ] && [ "$(echo "$BODY" | jq_ new_as_of)" \> "$BEFORE" ]; then
        ok "демо: прошла неделя ($BEFORE -> $(echo "$BODY" | jq_ new_as_of), +$(echo "$BODY" | jq_ added) операций, расчёт $(echo "$BODY" | jq_ recalc_ms) мс)"
      else
        bad "демо advance-week: $DCODE ${BODY:0:300}"
      fi
    else
      echo "skip демо: выключено или лента закончилась (чистая БД + seed_demo.sh вернут неделю)"
    fi
  else
    echo "skip демо: нет входа demo@example.com (DEMO_SEED=1?)"
  fi
  rm -f "$DJAR"
else
  echo "skip демо: demo_feed недоступен"
fi

# почта: письмо подтверждения после регистрации, подтверждение по ссылке, тестовое письмо, отчёт недели письмом (через Mailpit)
MAILPIT=${MAILPIT:-http://127.0.0.1:8025}
if curl -fsS "$MAILPIT/api/v1/messages?limit=1" >/dev/null 2>&1; then
  mail_link() {  # токен из последней ссылки /$1?token=... в письме на $EMAIL
    local id=""
    for _ in $(seq 1 20); do
      id=$(curl -s "$MAILPIT/api/v1/search?query=to:$EMAIL" | python3 -c 'import sys,json; m=json.load(sys.stdin)["messages"]; print(m[0]["ID"] if m else "")')
      [ -n "$id" ] && break
      sleep 0.5
    done
    [ -n "$id" ] && curl -s "$MAILPIT/api/v1/message/$id" | python3 -c 'import sys,json,re; t=json.load(sys.stdin)["Text"]; r=re.findall(r"/'"$1"'\?token=([A-Za-z0-9_-]+)", t); print(r[-1] if r else "")'
  }
  TOKEN=$(mail_link verify-email)
  [ -n "$TOKEN" ] && ok "письмо подтверждения пришло" || bad "нет письма подтверждения в Mailpit"
  req GET /api/v1/auth/me; [ "$(echo "$BODY" | jq_ email_verified)" = false ] && ok "почта ещё не подтверждена" || bad "email_verified до подтверждения"
  req PUT /api/v1/account/notifications '{"alerts":true}'; expect "рассылки без подтверждения" 409
  req POST /api/v1/account/email/verify "{\"token\":\"$TOKEN\"}"; expect "подтверждение почты" 200
  req POST /api/v1/account/email/verify "{\"token\":\"$TOKEN\"}"; expect "повторная ссылка" 404
  req GET /api/v1/auth/me; [ "$(echo "$BODY" | jq_ email_verified)" = true ] && ok "почта подтверждена" || bad "email_verified после подтверждения"
  req PUT /api/v1/account/notifications '{"alerts":true,"reports":true}'; expect "настройки рассылок" 200
  req POST /api/v1/account/notifications/test; expect "тестовое письмо" 200
  req POST /api/v1/auth/password/forgot "{\"email\":\"nobody-$RANDOM@example.com\"}"; expect "восстановление: неизвестный адрес" 202
else
  echo "skip почта: Mailpit недоступен ($MAILPIT)"
fi

req POST /api/v1/auth/logout; expect "logout" 204
req GET /api/v1/auth/me; expect "me после выхода" 401
[ $FAIL = 0 ] && echo "SMOKE PASSED" || { echo "SMOKE FAILED"; exit 1; }
