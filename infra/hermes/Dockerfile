# Hermes Agent (Nous Research) — ИИ-консультант. Модель — по подписке ChatGPT/Codex (OAuth, provider openai-codex),
# инструменты — только наши, по MCP из сервиса advisor. Встроенные инструменты (терминал, файлы, браузер, память) выключены.
FROM python:3.12-slim
RUN pip install --no-cache-dir "hermes-agent[mcp]==0.19.0" "aiohttp==3.14.1" \
 && useradd -u 1000 -m hermes
COPY infra/hermes/config.yaml /opt/hermes/config.yaml
COPY infra/hermes/entrypoint.sh /opt/hermes/entrypoint.sh
USER hermes
ENV HOME=/home/hermes HERMES_HOME=/data API_SERVER_ENABLED=true API_SERVER_HOST=0.0.0.0 API_SERVER_PORT=8642 PYTHONUNBUFFERED=1
ENTRYPOINT ["sh", "/opt/hermes/entrypoint.sh"]
