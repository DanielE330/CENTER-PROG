# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS build
RUN pip install --no-cache-dir uv==0.13.5
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY py py
COPY services/categorizer services/categorizer
COPY services/forecast services/forecast
COPY services/connectors services/connectors
COPY services/advisor services/advisor
ARG SERVICE
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --no-dev --no-editable --package "${SERVICE}" \
 && mkdir -p /app/migrations \
 && if [ -d "services/${SERVICE}/migrations" ]; then cp services/${SERVICE}/migrations/*.sql /app/migrations/; fi

FROM python:3.12-slim-bookworm
ARG SERVICE
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /venv /venv
COPY --from=build /app/migrations /app/migrations
COPY rules /app/rules
ENV SERVICE=${SERVICE} MIGRATIONS_DIR=/app/migrations RULES_DIR=/app/rules \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["/bin/sh", "-c", "exec /venv/bin/python -m \"$SERVICE\""]
