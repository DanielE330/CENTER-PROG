#!/usr/bin/env bash
# Запуск без Docker: auth :8001, parser :8002, gov :8003, Rust API :3000 (+ internal :3100). Остановка: infra/scripts/dev.sh stop
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN=data/run; mkdir -p "$RUN"
if [ "${1:-}" = "stop" ]; then
  for f in "$RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done
  # uv запускает uvicorn дочерним процессом: добиваем сервисы по портам
  for port in 3000 3100 8001 8002 8003; do
    pid=$(ss -ltnp 2>/dev/null | grep ":$port " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  exit 0
fi
export RUSTUP_TOOLCHAIN=${RUSTUP_TOOLCHAIN:-stable}
(cd web && [ -d node_modules ] || npm ci >/dev/null; npm run build >/dev/null)
(cd backend/api && cargo build --release 2>&1 | tail -1)
ROOT=$PWD
(cd backend/services && AUTH_DB=$ROOT/$RUN/auth.db DEMO_SEED=1 setsid nohup uv run uvicorn auth.app:app --host 127.0.0.1 --port 8001 >$ROOT/$RUN/auth.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/auth.pid)
(cd backend/services && setsid nohup uv run uvicorn parser.app:app --host 127.0.0.1 --port 8002 >$ROOT/$RUN/parser.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/parser.pid)
(cd backend/services && GOV_CACHE_DIR=$ROOT/$RUN/gov_cache setsid nohup uv run uvicorn gov.app:app --host 127.0.0.1 --port 8003 >$ROOT/$RUN/gov.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/gov.pid)
DB_PATH=$RUN/core.db WEB_DIR=web/dist setsid nohup backend/api/target/release/api >$RUN/api.log 2>&1 < /dev/null &
echo $! >$RUN/api.pid
sleep 3; curl -fsS localhost:3000/live && echo " -> http://localhost:3000  (demo@example.com / demo12345)"
