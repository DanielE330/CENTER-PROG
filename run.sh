#!/usr/bin/env bash
# Запуск всего сервиса одной командой. Нужен только Docker (compose v2).
#   ./run.sh            собрать, запустить, загрузить демо-данные
#   ./run.sh stop       остановить (данные сохраняются)
#   ./run.sh reset      остановить и удалить данные (тома)
#   ./run.sh logs [svc] логи
#   ./run.sh status     состояние контейнеров
# Первая установка «под ключ» (секреты, оператор с обязательной 2FA): ./install.sh
# Демо «Прошла неделя»: выписка демо-пользователя грузится до DEMO_CUTOFF (infra/.env, по умолчанию 2026-07-12), остальное отдаёт demo-feed.
#   Если демо-пользователь уже получил выписку целиком (старый стенд), начните заново: ./run.sh reset && ./run.sh
# Настройки: infra/.env.example (скопируйте в infra/.env). Полный датасет: DATASET_DIR=../data/dataset ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
C="docker compose -f infra/docker-compose.yml"

need_docker() {
  command -v docker >/dev/null || { echo "Не найден docker. Установите Docker: https://docs.docker.com/get-docker/"; exit 1; }
  docker compose version >/dev/null 2>&1 || { echo "Нужен Docker Compose v2 (команда 'docker compose')."; exit 1; }
  if ! docker info >/dev/null 2>&1; then
    echo "Docker недоступен. Проверьте, что демон запущен (Linux: sudo systemctl start docker)"
    echo "и что ваш пользователь в группе docker (sudo usermod -aG docker \$USER, затем перелогиньтесь)."
    exit 1
  fi
}

port() { grep -E '^GATEWAY_PORT=' infra/.env 2>/dev/null | cut -d= -f2 | sed 's/[[:space:]#].*//' || true; }
# дата отсечки демо: seed_demo.sh и контейнер demo-feed должны видеть одно и то же значение
if [ -z "${DEMO_CUTOFF:-}" ]; then
  DEMO_CUTOFF=$(grep -E '^DEMO_CUTOFF=' infra/.env 2>/dev/null | cut -d= -f2 | sed 's/[[:space:]#].*//' || true)
fi
export DEMO_CUTOFF=${DEMO_CUTOFF:-2026-07-12}
# пароль демо-аккаунтов: seed_demo.sh и контейнеры должны видеть одно значение (по умолчанию demo12345)
if [ -z "${DEMO_PASSWORD:-}" ]; then
  DEMO_PASSWORD=$(grep -E '^DEMO_PASSWORD=' infra/.env 2>/dev/null | cut -d= -f2- | sed 's/[[:space:]]#.*//;s/[[:space:]]*$//' || true)
fi
export DEMO_PASSWORD=${DEMO_PASSWORD:-demo12345}

# Ключ шифрования паролей интеграций («Настройки → Интеграции»): создаётся один раз в infra/.env, существующий не меняется.
ensure_integrations_key() {
  local env=infra/.env key
  grep -qE '^INTEGRATIONS_KEY=[^[:space:]#]+' "$env" 2>/dev/null && return 0
  key=$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')
  [ -f "$env" ] || { cp infra/.env.example "$env"; chmod 600 "$env"; }
  if grep -qE '^INTEGRATIONS_KEY=' "$env"; then sed -i.bak "s|^INTEGRATIONS_KEY=.*|INTEGRATIONS_KEY=$key|" "$env"; rm -f "$env.bak"
  else printf 'INTEGRATIONS_KEY=%s\n' "$key" >> "$env"; fi
  echo "создан ключ INTEGRATIONS_KEY в $env (не теряйте его: им зашифрованы пароли 1С)"
}

case "${1:-up}" in
  up)
    need_docker
    ensure_integrations_key
    $C up --build -d
    P=$(port); P=${P:-3000}
    printf 'Ждём запуска'
    for _ in $(seq 1 60); do
      curl -fsS "http://localhost:$P/live" >/dev/null 2>&1 && break
      printf '.'; sleep 2
    done
    echo
    DEMO=$(grep -E '^DEMO_SEED=' infra/.env 2>/dev/null | cut -d= -f2 | cut -d' ' -f1 || true)
    if [ "${DEMO:-1}" != "0" ]; then
      bash infra/scripts/seed_demo.sh "http://localhost:$P" || echo "(демо-данные не загружены, можно повторить: infra/scripts/seed_demo.sh)"
    fi
    echo
    echo "Готово: http://localhost:$P"
    if [ "${DEMO:-1}" != "0" ]; then
      echo "Демо-вход: на странице входа блок «Тестовый вход» (Директор / Редактор / Наблюдатель), либо demo@example.com / $DEMO_PASSWORD"
    fi
    echo "Письма (Mailpit): http://localhost:8025"
    ;;
  stop)   need_docker; $C down ;;
  reset)  need_docker; $C down -v ;;
  logs)   need_docker; shift; $C logs -f --tail=100 "$@" ;;
  status) need_docker; $C ps ;;
  *) echo "Использование: ./run.sh [up|stop|reset|logs|status]"; exit 1 ;;
esac
