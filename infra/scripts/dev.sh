#!/usr/bin/env bash
# Запуск без Docker: auth :8001, parser :8002, gov :8003, ecp :8007, onec :8004, edo :8005, bank :8006, integrator :8008, notifier :8009, demo_feed :8012, llm_gateway :8010, advisor :8011, backoffice :8013, notify :8014,
# Rust: ledger :3002, forecast :3003, planfact :3004, api :3000 (+ internal :3100). Остановка: infra/scripts/dev.sh stop
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN=data/run; mkdir -p "$RUN"
if [ "${1:-}" = "stop" ]; then
  for f in "$RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done
  # uv запускает uvicorn дочерним процессом: добиваем сервисы по портам
  for port in 3000 3100 3002 3003 3004 8001 8002 8003 8004 8005 8006 8007 8008 8009 8010 8011 8012 8013 8014; do
    pid=$(ss -ltnp 2>/dev/null | grep ":$port " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  exit 0
fi
export RUSTUP_TOOLCHAIN=${RUSTUP_TOOLCHAIN:-stable}
(cd frontend && [ -d node_modules ] || npm ci >/dev/null; npm run build >/dev/null)
(cd backend && cargo build --release --workspace --bins 2>&1 | tail -1)
ROOT=$PWD
# одна «сегодняшняя» дата для имитаторов и integrator: флаги просрочки согласованы (по умолчанию последняя дата выписок)
MOCK_TODAY=${MOCK_TODAY:-2026-09-29}
(cd backend/services && AUTH_DB=$ROOT/$RUN/auth.db DEMO_SEED=1 setsid nohup uv run uvicorn auth.app:app --host 127.0.0.1 --port 8001 >$ROOT/$RUN/auth.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/auth.pid)
(cd backend/services && setsid nohup uv run uvicorn parser.app:app --host 127.0.0.1 --port 8002 >$ROOT/$RUN/parser.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/parser.pid)
(cd backend/services && GOV_CACHE_DIR=$ROOT/$RUN/gov_cache setsid nohup uv run uvicorn gov.app:app --host 127.0.0.1 --port 8003 >$ROOT/$RUN/gov.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/gov.pid)
for spec in ecp:8007 onec:8004 edo:8005 bank:8006 integrator:8008; do
  svc=${spec%%:*}; port=${spec##*:}
  (cd backend/services && ECP_DB=$ROOT/$RUN/ecp.db AUTH_URL=http://127.0.0.1:8001 MOCK_TODAY=$MOCK_TODAY INTEGRATOR_TODAY=$MOCK_TODAY ONEC_URL=http://127.0.0.1:8004 EDO_URL=http://127.0.0.1:8005 BANK_URL=http://127.0.0.1:8006 setsid nohup uv run uvicorn $svc.app:app --host 127.0.0.1 --port $port >$ROOT/$RUN/$svc.log 2>&1 < /dev/null &
   echo $! >$ROOT/$RUN/$svc.pid)
done
(cd backend/services && DEMO_DATASET_DIR=${DEMO_DATASET_DIR:-} DEMO_CUTOFF=${DEMO_CUTOFF:-2026-07-12} setsid nohup uv run uvicorn demo_feed.app:app --host 127.0.0.1 --port 8012 >$ROOT/$RUN/demo_feed.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/demo_feed.pid)
BIN=backend/target/release
LEDGER_URL=http://127.0.0.1:3002; FORECAST_URL=http://127.0.0.1:3003; PLANFACT_URL=http://127.0.0.1:3004
DB_PATH=$RUN/ledger.db INTERNAL_ADDR=127.0.0.1:3002 PARSER_URL=http://127.0.0.1:8002 setsid nohup $BIN/ledger >$RUN/ledger.log 2>&1 < /dev/null &
echo $! >$RUN/ledger.pid
DB_PATH=$RUN/forecast.db INTERNAL_ADDR=127.0.0.1:3003 LEDGER_URL=$LEDGER_URL setsid nohup $BIN/forecast >$RUN/forecast.log 2>&1 < /dev/null &
echo $! >$RUN/forecast.pid
INTERNAL_ADDR=127.0.0.1:3004 LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL setsid nohup $BIN/planfact >$RUN/planfact.log 2>&1 < /dev/null &
echo $! >$RUN/planfact.pid
(cd backend/services && NOTIFIER_DB=$ROOT/$RUN/notifier.db TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-} TELEGRAM_BOT_USERNAME=${TELEGRAM_BOT_USERNAME:-} PUBLIC_URL=${PUBLIC_URL:-http://localhost:3000} HERMES_URL=${HERMES_URL:-} HERMES_MODEL=${HERMES_MODEL:-NousResearch/Hermes-3-Llama-3.1-8B} HERMES_API_KEY=${HERMES_API_KEY:-} NOTIFY_URL=http://127.0.0.1:8014 \
 LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL PLANFACT_URL=$PLANFACT_URL setsid nohup uv run uvicorn notifier.app:app --host 127.0.0.1 --port 8009 >$ROOT/$RUN/notifier.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/notifier.pid)
# llm_gateway (:8010): единственный выход к облачным LLM. Ключи и провайдер берутся из окружения оболочки
# (LLM_PROVIDER, LLM_MODEL, ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY); без них консультант отвечает шаблоном.
(cd backend/services && LLM_GATEWAY_DB=$ROOT/$RUN/llm_gateway.db setsid nohup uv run uvicorn llm_gateway.app:app --host 127.0.0.1 --port 8010 >$ROOT/$RUN/llm_gateway.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/llm_gateway.pid)
(cd backend/services && LLM_GATEWAY_URL=http://127.0.0.1:8010 LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL PLANFACT_URL=$PLANFACT_URL setsid nohup uv run uvicorn advisor.app:app --host 127.0.0.1 --port 8011 >$ROOT/$RUN/advisor.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/advisor.pid)
# backoffice (:8013): панель оператора платформы (/operator). Первый оператор: OPERATOR_EMAIL и OPERATOR_PASSWORD в окружении оболочки
# (создаётся при старте, если такого email ещё нет) или `cd backend/services && BACKOFFICE_DB=../../data/run/backoffice.db uv run python -m backoffice.create_operator <email>`.
(cd backend/services && BACKOFFICE_DB=$ROOT/$RUN/backoffice.db AUTH_URL=http://127.0.0.1:8001 LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL setsid nohup uv run uvicorn backoffice.app:app --host 127.0.0.1 --port 8013 >$ROOT/$RUN/backoffice.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/backoffice.pid)
# notify (:8014): письма. Без SMTP_HOST письма идут на localhost:1025 — например, Mailpit: docker run -p 1025:1025 -p 8025:8025 axllent/mailpit:v1.31.6
(cd backend/services && NOTIFY_DB=$ROOT/$RUN/notify.db AUTH_URL=http://127.0.0.1:8001 LEDGER_URL=$LEDGER_URL SMTP_HOST=${SMTP_HOST:-127.0.0.1} setsid nohup uv run uvicorn notify.app:app --host 127.0.0.1 --port 8014 >$ROOT/$RUN/notify.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/notify.pid)
sleep 1
WEB_DIR=frontend/dist LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL PLANFACT_URL=$PLANFACT_URL \
  AUTH_URL=http://127.0.0.1:8001 PARSER_URL=http://127.0.0.1:8002 GOV_URL=http://127.0.0.1:8003 ECP_URL=http://127.0.0.1:8007 INTEGRATOR_URL=http://127.0.0.1:8008 NOTIFIER_URL=http://127.0.0.1:8009 \
  ADVISOR_URL=http://127.0.0.1:8011 DEMO_FEED_URL=http://127.0.0.1:8012 DEMO_FEED_ENABLED=${DEMO_FEED_ENABLED:-1} BACKOFFICE_URL=http://127.0.0.1:8013 NOTIFY_URL=http://127.0.0.1:8014 \
  setsid nohup $BIN/api >$RUN/api.log 2>&1 < /dev/null &
echo $! >$RUN/api.pid
sleep 3; curl -fsS localhost:3000/live && echo " -> http://localhost:3000  (demo@example.com / demo12345)"
