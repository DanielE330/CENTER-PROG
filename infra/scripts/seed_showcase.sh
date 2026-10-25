#!/usr/bin/env bash
# Наполняет демо-компанию так, чтобы на графиках была видна вся функциональность (запускается после seed_demo.sh):
#  - обязательства из 1С и ЭДО (синхронизация интеграций), налоговый календарь и проверка контрагентов (ФНС);
#  - плановые платежи: часть совпадёт с операциями ленты «Прошла неделя» (план-факт: «исполнено»), один обещанный платёж
#    не придёт (предупреждение о просрочке и отклонение), крупная покупка создаёт провал остатка и риск разрыва.
# Повторный запуск безопасен: события с теми же названиями не дублируются. Запуск: infra/scripts/seed_showcase.sh [BASE_URL]
set -uo pipefail
BASE=${1:-http://localhost:3000}
PASS=${DEMO_PASSWORD:-demo12345}
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
api() { curl -sS -m 90 -b "$JAR" -H 'content-type: application/json' "$@"; }
ME=$(curl -fsS -m 30 -c "$JAR" -X POST "$BASE/api/v1/auth/login" -H 'content-type: application/json' \
  -d "{\"email\":\"demo@example.com\",\"password\":\"$PASS\"}") || { echo "не удалось войти демо-директором"; exit 1; }
CID=$(printf '%s' "$ME" | python3 -c 'import json,sys; print(json.load(sys.stdin)["company_id"])')
C="$BASE/api/v1/companies/$CID"
EXIST=$(api "$C/expected-events" | python3 -c 'import json,sys; print("\n".join(e.get("title","") for e in json.load(sys.stdin).get("items",[])))')

add_event() { # дата сумма_в_рублях направление(in|out) название контрагент источник(manual|invoice|contract|tax) уверенность
  if printf '%s\n' "$EXIST" | grep -qxF -- "$4"; then echo "уже есть: $4"; return; fi
  local body
  body=$(python3 - "$@" <<'PY'
import json, sys
d, rub, direction, title, cp, src, conf = sys.argv[1:8]
print(json.dumps({"date": d, "amount_kop": str(round(float(rub) * 100)), "direction": direction, "title": title,
                  "counterparty": cp, "source": src, "confidence": float(conf)}))
PY
)
  if api -o /dev/null -w '%{http_code}' -X POST "$C/expected-events" -d "$body" | grep -q '^201$'; then echo "добавлено: $4"; else echo "не добавлено: $4"; fi
}

echo "== интеграции (1С и ЭДО)"
api -X POST "$C/integrations/sync" -d '{"external_company":"C001"}' | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print("создано:", d.get("created"), "пропущено:", d.get("skipped"))
except Exception: print("синхронизация недоступна")'

echo "== налоговый календарь"
api -X POST "$C/expected-events/tax-calendar" -d '{}' | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print("создано:", d.get("created") or d.get("error",{}).get("message"))
except Exception: print("недоступно")'
api -X POST "$C/integrations/fns/sync" -d '{}' >/dev/null 2>&1 && echo "ФНС: синхронизировано" || echo "ФНС: пропущено"
api "$C/fns/counterparties?top=20" >/dev/null 2>&1 && echo "контрагенты проверены"

echo "== плановые платежи для план-факта и риска"
# совпадут с операциями ленты (06.08 и 12.08.2026): в план-факте будут «исполнено»
add_event 2026-08-06 102862.05 in  "Плановая оплата от Дистрибьютор-307"        "ООО «Дистрибьютор-307»"   manual   0.9
add_event 2026-08-12 109239.50 in  "Плановая оплата по счёту 872"               "ООО «Дистрибьютор-307»"   invoice  0.9
add_event 2026-08-10 348216.40 out "Плановая выплата зарплаты (расчёт)"         "ПАО «Банк»"               manual   0.95
# обещанный клиентом платёж, который не придёт: просрочка и отклонение в план-факте
add_event 2026-08-14 450000.00 in  "Клиент обещал заплатить (не поступило)"     "ООО «Строймаркет»"        manual   0.7
# крупная покупка: провал остатка на графике прогноза, риск кассового разрыва
add_event 2026-08-20 1800000.00 out "Покупка оборудования (рассрочки нет)"      "ООО «Станкомаш»"          contract 1.0
add_event 2026-09-05 650000.00 out "Страховой платёж по договору"               "АО «Страхование»"         contract 1.0
echo "готово: откройте «Обзор», «Прогноз», «План и факт» и «Платежи»"
