#!/usr/bin/env bash
# Запуск без Docker: auth :8001, parser :8002, gov :8003, ecp :8007, onec :8004, edo :8005, bank :8006, integrator :8008, notifier :8009,
# Rust: ledger :3002, forecast :3003, planfact :3004, api :3000 (+ internal :3100). Остановка: infra/scripts/dev.sh stop
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN=data/run; mkdir -p "$RUN"
if [ "${1:-}" = "stop" ]; then
  for f in "$RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done
  # uv запускает uvicorn дочерним процессом: добиваем сервисы по портам
  for port in 3000 3100 3002 3003 3004 8001 8002 8003 8004 8005 8006 8007 8008 8009; do
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
BIN=backend/target/release
LEDGER_URL=http://127.0.0.1:3002; FORECAST_URL=http://127.0.0.1:3003; PLANFACT_URL=http://127.0.0.1:3004
DB_PATH=$RUN/ledger.db INTERNAL_ADDR=127.0.0.1:3002 PARSER_URL=http://127.0.0.1:8002 setsid nohup $BIN/ledger >$RUN/ledger.log 2>&1 < /dev/null &
echo $! >$RUN/ledger.pid
DB_PATH=$RUN/forecast.db INTERNAL_ADDR=127.0.0.1:3003 LEDGER_URL=$LEDGER_URL setsid nohup $BIN/forecast >$RUN/forecast.log 2>&1 < /dev/null &
echo $! >$RUN/forecast.pid
INTERNAL_ADDR=127.0.0.1:3004 LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL setsid nohup $BIN/planfact >$RUN/planfact.log 2>&1 < /dev/null &
echo $! >$RUN/planfact.pid
(cd backend/services && NOTIFIER_DB=$ROOT/$RUN/notifier.db TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN:-} TELEGRAM_BOT_USERNAME=${TELEGRAM_BOT_USERNAME:-} PUBLIC_URL=${PUBLIC_URL:-http://localhost:3000} HERMES_URL=${HERMES_URL:-} HERMES_MODEL=${HERMES_MODEL:-NousResearch/Hermes-3-Llama-3.1-8B} HERMES_API_KEY=${HERMES_API_KEY:-} SMTP_HOST=${SMTP_HOST:-} SMTP_PORT=${SMTP_PORT:-587} SMTP_USER=${SMTP_USER:-} SMTP_PASSWORD=${SMTP_PASSWORD:-} SMTP_FROM=${SMTP_FROM:-} \
 LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL PLANFACT_URL=$PLANFACT_URL setsid nohup uv run uvicorn notifier.app:app --host 127.0.0.1 --port 8009 >$ROOT/$RUN/notifier.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/notifier.pid)
sleep 1
WEB_DIR=frontend/dist LEDGER_URL=$LEDGER_URL FORECAST_URL=$FORECAST_URL PLANFACT_URL=$PLANFACT_URL \
  AUTH_URL=http://127.0.0.1:8001 PARSER_URL=http://127.0.0.1:8002 GOV_URL=http://127.0.0.1:8003 ECP_URL=http://127.0.0.1:8007 INTEGRATOR_URL=http://127.0.0.1:8008 NOTIFIER_URL=http://127.0.0.1:8009 \
  setsid nohup $BIN/api >$RUN/api.log 2>&1 < /dev/null &
echo $! >$RUN/api.pid
sleep 3; curl -fsS localhost:3000/live && echo " -> http://localhost:3000  (demo@example.com / demo12345)"
