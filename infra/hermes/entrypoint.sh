#!/bin/sh
# Конфигурация из образа каждый раз заменяет конфиг в томе (права консультанта задаются здесь, а не руками в томе).
# В томе /data остаются только учётные данные подписки (auth.json) и служебные файлы Hermes.
set -e
# модель подставляется здесь: в model.default Hermes ${VAR} не раскрывает
sed "s|\${HERMES_MODEL}|${HERMES_MODEL:-gpt-6.1-sol}|" /opt/hermes/config.yaml > "$HERMES_HOME/config.yaml"
if [ ! -s "$HERMES_HOME/auth.json" ]; then
  echo "Нет входа в Codex: docker compose -f infra/docker-compose.yml --profile hermes run --rm hermes-login" >&2
fi
# модели подписки и уровни размышления: псевдонимы model_routes вида gpt-6.1-sol@high
python /opt/hermes/routes.py "$HERMES_HOME/config.yaml" || true
exec python /opt/hermes/launch.py
