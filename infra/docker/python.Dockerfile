# syntax=docker/dockerfile:1
# Build context: repo root. Targets: auth, parser, gov, ecp, onec, edo, bank, integrator, notifier, demo_feed, llm_gateway, advisor, backoffice, notify, fns, test.
ARG SVC=auth
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY backend/services/pyproject.toml backend/services/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/services/ ./
RUN useradd --system --uid 10001 --no-create-home app && mkdir /data && chown app /data
VOLUME /data

FROM base AS auth
ENV AUTH_DB=/data/auth.db
USER app
EXPOSE 8001
CMD ["uvicorn", "auth.app:app", "--host", "0.0.0.0", "--port", "8001"]

FROM base AS parser
# scikit-learn нужен только парсеру: модель на хвост категорий (parser/tail.py)
RUN uv sync --frozen --no-dev --group tail
USER app
EXPOSE 8002
CMD ["uvicorn", "parser.app:app", "--host", "0.0.0.0", "--port", "8002"]

FROM base AS gov
ENV GOV_CACHE_DIR=/tmp/gov_cache
USER app
EXPOSE 8003
CMD ["uvicorn", "gov.app:app", "--host", "0.0.0.0", "--port", "8003"]

FROM base AS ecp
ENV ECP_DB=/data/ecp.db
USER app
EXPOSE 8007
CMD ["uvicorn", "ecp.app:app", "--host", "0.0.0.0", "--port", "8007"]

FROM base AS onec
USER app
EXPOSE 8004
CMD ["uvicorn", "onec.app:app", "--host", "0.0.0.0", "--port", "8004"]

FROM base AS edo
USER app
EXPOSE 8005
CMD ["uvicorn", "edo.app:app", "--host", "0.0.0.0", "--port", "8005"]

FROM base AS bank
USER app
EXPOSE 8006
CMD ["uvicorn", "bank.app:app", "--host", "0.0.0.0", "--port", "8006"]

FROM base AS integrator
USER app
EXPOSE 8008
CMD ["uvicorn", "integrator.app:app", "--host", "0.0.0.0", "--port", "8008"]

FROM base AS notifier
# read_only-ФС и пользователь без домашнего каталога: кэш matplotlib кладём в tmpfs
ENV NOTIFIER_DB=/data/notifier.db MPLCONFIGDIR=/tmp/mpl
USER app
EXPOSE 8009
CMD ["uvicorn", "notifier.app:app", "--host", "0.0.0.0", "--port", "8009"]
FROM base AS demo_feed
USER app
EXPOSE 8012
CMD ["uvicorn", "demo_feed.app:app", "--host", "0.0.0.0", "--port", "8012"]

FROM base AS llm_gateway
ENV LLM_GATEWAY_DB=/data/llm_gateway.db
USER app
EXPOSE 8010
CMD ["uvicorn", "llm_gateway.app:app", "--host", "0.0.0.0", "--port", "8010"]

FROM base AS advisor
USER app
EXPOSE 8011
CMD ["uvicorn", "advisor.app:app", "--host", "0.0.0.0", "--port", "8011"]

FROM base AS backoffice
ENV BACKOFFICE_DB=/data/backoffice.db
USER app
EXPOSE 8013
CMD ["uvicorn", "backoffice.app:app", "--host", "0.0.0.0", "--port", "8013"]

FROM base AS notify
ENV NOTIFY_DB=/data/notify.db
USER app
EXPOSE 8014
CMD ["uvicorn", "notify.app:app", "--host", "0.0.0.0", "--port", "8014"]

FROM base AS fns
# сведения ФНС: ЕНС (из справки), налоговый календарь, проверка контрагентов по открытым реестрам
ENV FNS_DB=/data/fns.db
USER app
EXPOSE 8015
CMD ["uvicorn", "fns.app:app", "--host", "0.0.0.0", "--port", "8015"]

FROM base AS test
RUN uv sync --frozen
CMD ["pytest", "-q"]
