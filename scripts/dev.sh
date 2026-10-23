#!/usr/bin/env bash
# Запуск без Docker: auth :8001, parser :8002, Rust API :3000 (+ internal :3100). Остановка: scripts/dev.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=data/run; mkdir -p "$RUN"
if [ "${1:-}" = "stop" ]; then
  for f in "$RUN"/*.pid; do [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; done; exit 0
fi
export RUSTUP_TOOLCHAIN=${RUSTUP_TOOLCHAIN:-stable}
(cd web && [ -d node_modules ] || npm ci >/dev/null; npm run build >/dev/null)
(cd crates/api && cargo build --release 2>&1 | tail -1)
ROOT=$PWD
(cd services && AUTH_DB=$ROOT/$RUN/auth.db DEMO_SEED=1 setsid nohup uv run uvicorn auth.app:app --host 127.0.0.1 --port 8001 >$ROOT/$RUN/auth.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/auth.pid)
(cd services && setsid nohup uv run uvicorn parser.app:app --host 127.0.0.1 --port 8002 >$ROOT/$RUN/parser.log 2>&1 < /dev/null &
 echo $! >$ROOT/$RUN/parser.pid)
DB_PATH=$RUN/core.db WEB_DIR=web/dist setsid nohup crates/api/target/release/api >$RUN/api.log 2>&1 < /dev/null &
echo $! >$RUN/api.pid
sleep 3; curl -fsS localhost:3000/live && echo " -> http://localhost:3000  (demo@example.com / demo12345)"
