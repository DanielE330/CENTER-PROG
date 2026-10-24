# syntax=docker/dockerfile:1
# Build context: repo root. Targets: auth, parser, gov, ecp, onec, edo, bank, integrator, test.
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

FROM base AS test
RUN uv sync --frozen
CMD ["pytest", "-q"]
