#!/usr/bin/env bash
# Запуск всего сервиса одной командой. Нужен только Docker (compose v2).
#   ./run.sh            собрать, запустить, загрузить демо-данные
#   ./run.sh stop       остановить (данные сохраняются)
#   ./run.sh reset      остановить и удалить данные (тома)
#   ./run.sh logs [svc] логи
#   ./run.sh status     состояние контейнеров
# Первая установка «под ключ» (секреты, оператор с обязательной 2FA): ./install.sh
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

port() { grep -E '^GATEWAY_PORT=' infra/.env 2>/dev/null | cut -d= -f2 || true; }

case "${1:-up}" in
  up)
    need_docker
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
    if [ "${DEMO:-1}" != "0" ]; then echo "Демо-вход: demo@example.com / demo12345"; fi
    ;;
  stop)   need_docker; $C down ;;
  reset)  need_docker; $C down -v ;;
  logs)   need_docker; shift; $C logs -f --tail=100 "$@" ;;
  status) need_docker; $C ps ;;
  *) echo "Использование: ./run.sh [up|stop|reset|logs|status]"; exit 1 ;;
esac
