#!/bin/sh
# Конфигурация из образа каждый раз заменяет конфиг в томе (права консультанта задаются здесь, а не руками в томе).
# В томе /data остаются только учётные данные подписки (auth.json) и служебные файлы Hermes.
set -e
cp /opt/hermes/config.yaml "$HERMES_HOME/config.yaml"
if [ ! -s "$HERMES_HOME/auth.json" ]; then
  echo "Нет входа в Codex: docker compose -f infra/docker-compose.yml run --rm hermes-login" >&2
fi
exec hermes gateway run
