#!/usr/bin/env bash
# Установка «под ключ»: ./install.sh [--email EMAIL] [--port PORT] [--domain HOST] [--demo]
#   создаёт infra/.env с новыми секретами (если файла нет; существующий не перезаписывается),
#   поднимает сервисы (./run.sh up), создаёт оператора платформы (страница /operator) с обязательной 2FA
#   и печатает ссылку на страницу оператора, логин и пароль.
# Можно запускать повторно: секреты, оператор и его пароль сохраняются.
# Необязательные переменные окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, HERMES_URL, HERMES_API_KEY,
#   OPERATOR_EMAIL. Токен бота в репозиторий не попадает: он записывается только в infra/.env (в .gitignore).
set -euo pipefail
cd "$(dirname "$0")"

EMAIL=${OPERATOR_EMAIL:-operator@local.test}; PORT=""; DOMAIN=""; DEMO=0
while [ $# -gt 0 ]; do
  case "$1" in
    --email)  EMAIL=${2:?нужен email}; shift 2 ;;
    --port)   PORT=${2:?нужен порт}; shift 2 ;;
    --domain) DOMAIN=${2:?нужен домен}; shift 2 ;;
    --demo)   DEMO=1; shift ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный параметр: $1 (см. ./install.sh --help)"; exit 1 ;;
  esac
done

ENV=infra/.env; CRED=infra/.operator-credentials
say() { printf '\n== %s\n' "$*"; }
rand() {
  if command -v openssl >/dev/null; then openssl rand -base64 "$1" | tr -d '/+=\n'
  else python3 -c 'import secrets,sys; print(secrets.token_urlsafe(int(sys.argv[1])))' "$1" | tr -d '-_\n'; fi
}
get_kv() { grep -E "^$1=" "$ENV" 2>/dev/null | head -1 | cut -d= -f2- | cut -d' ' -f1 || true; }
# set_kv KEY VALUE: задаёт ключ, если его нет или он пустой (заполненные значения не трогаем)
set_kv() {
  if [ -n "$(get_kv "$1")" ]; then return 0; fi
  if grep -qE "^$1=" "$ENV" 2>/dev/null; then
    V=$2 python3 - "$ENV" "$1" <<'PY'
import os, re, sys
path, key = sys.argv[1], sys.argv[2]
s = open(path).read()
s = re.sub(rf"^{re.escape(key)}=[^\n#]*", lambda m: f"{key}={os.environ['V']}" + (" " if "#" in s[m.end():m.end() + 1] else ""), s, count=1, flags=re.M)
open(path, "w").write(s)
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV"
  fi
}

say "Проверка Docker"
command -v docker >/dev/null || { echo "Не найден docker. Установите: https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Нужен Docker Compose v2 (команда 'docker compose')."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker недоступен: запустите демон (Linux: sudo systemctl start docker) и добавьте себя в группу docker."; exit 1; }
command -v curl >/dev/null || { echo "Нужен curl."; exit 1; }

say "Настройки (infra/.env)"
[ -f "$ENV" ] || { cp infra/.env.example "$ENV"; chmod 600 "$ENV"; echo "создан $ENV"; }
chmod 600 "$ENV"
[ -n "$PORT" ] && { grep -q '^GATEWAY_PORT=' "$ENV" && sed -i.bak "s/^GATEWAY_PORT=.*/GATEWAY_PORT=$PORT/" "$ENV" && rm -f "$ENV.bak"; }
if [ -n "$DOMAIN" ]; then
  for kv in "SITE_ADDRESS=$DOMAIN" "CADDY_AUTO_HTTPS=on" "COOKIE_SECURE=true"; do
    k=${kv%%=*}; sed -i.bak "s|^$k=.*|$kv|" "$ENV"; rm -f "$ENV.bak"
  done
fi
sed -i.bak "s/^DEMO_SEED=[^ ]*/DEMO_SEED=$DEMO/" "$ENV"; rm -f "$ENV.bak"
set_kv AUTH_MFA_KEY "$(rand 32)"           # ключ шифрования секретов 2FA пользователей компаний (необязательной, AUTH_REQUIRE_2FA=1 делает её обязательной)
set_kv AUTH_REQUIRE_2FA 0
set_kv BACKOFFICE_MFA_KEY "$(rand 32)"     # ключ шифрования секретов 2FA операторов; не теряйте: без него 2FA придётся сбросить
set_kv BACKOFFICE_REQUIRE_2FA 1            # второй фактор оператора обязателен
for v in TELEGRAM_BOT_USERNAME HERMES_URL HERMES_API_KEY; do
  [ -n "${!v:-}" ] && set_kv "$v" "${!v}"
done
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] && [ -z "$(get_kv TELEGRAM_BOT_TOKEN)" ] && [ -t 0 ]; then
  read -rsp "Токен Telegram-бота (Enter — пропустить, бот будет выключен): " TELEGRAM_BOT_TOKEN; echo
fi
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] && set_kv TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"

say "Запуск сервисов (первая сборка занимает несколько минут)"
./run.sh up

say "Оператор"
C="docker compose -f infra/docker-compose.yml"
PASS=""; [ -f "$CRED" ] && PASS=$(grep -E '^password=' "$CRED" | cut -d= -f2- || true)
[ -n "$PASS" ] || PASS=$(rand 18 | cut -c1-20)
# пароль уходит оператору через стандартный ввод: не в аргументах (ps), не в окружении контейнера и не в .env
if printf '%s\n' "$PASS" | $C exec -T backoffice python -m backoffice.create_operator --stdin "$EMAIL" >/dev/null 2>&1; then RES=created
else RES=exists; fi

PORT=$(get_kv GATEWAY_PORT); PORT=${PORT:-3000}
SITE=$(get_kv SITE_ADDRESS)
case "$SITE" in :*|"") URL="http://localhost:$PORT/operator" ;; *) URL="https://$SITE/operator" ;; esac
if [ "$RES" = "created" ]; then
  ( umask 077; printf 'url=%s\nlogin=%s\npassword=%s\n' "$URL" "$EMAIL" "$PASS" > "$CRED" )
fi

echo
echo "================ Установка завершена ================"
echo "Страница оператора: $URL"
echo "Логин:              $EMAIL"
if [ "$RES" = "created" ] || [ -f "$CRED" ]; then
  echo "Пароль:             $(grep -E '^password=' "$CRED" | cut -d= -f2-)"
  echo "(сохранён в $CRED с правами 600; файл не попадает в git)"
else
  echo "Пароль:             не менялся (оператор уже существовал)"
fi
echo
echo "При первом входе откройте страницу оператора, введите логин и пароль, отсканируйте QR в приложении-аутентификаторе"
echo "(Google Authenticator, Microsoft Authenticator и т.п.), введите код и сохраните резервные коды."
echo "Дальше вход только по паролю и коду из приложения."
echo "Потеряли телефон и резервные коды? Сбросить 2FA (при следующем входе настроится заново):"
echo "  docker compose -f infra/docker-compose.yml exec backoffice python -m backoffice.create_operator --reset-2fa $EMAIL"
echo
echo "Сведения ФНС (налоги на год, ЕНС, проверка контрагентов): режим sim, данные имитатора. Режим live выключен: docs/fns.md"
echo "Сайт для компаний: ${URL%/operator}  (вход и регистрация; пользователи компаний видят свою компанию, а не панель оператора)"
