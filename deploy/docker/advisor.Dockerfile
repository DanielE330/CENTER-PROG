# syntax=docker/dockerfile:1.7
# The advisor runs Hermes Agent (Python 3.14 only) pinned to a commit.
FROM python:3.14-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.13.5
ARG HERMES_REPO=https://github.com/NousResearch/hermes-agent
ARG HERMES_SHA=75f8bd95fe73d01a11aa63a85880c404060ffbbb
WORKDIR /opt/hermes
RUN git init . && git remote add origin "$HERMES_REPO" \
 && git fetch --depth 1 origin "$HERMES_SHA" && git checkout FETCH_HEAD && rm -rf .git
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY py py
COPY services/categorizer services/categorizer
COPY services/forecast services/forecast
COPY services/connectors services/connectors
COPY services/advisor services/advisor
# Hermes is installed exactly from its own lock file (its pyproject pins alone do not resolve),
# then our packages go on top of the same environment.
WORKDIR /opt/hermes
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/venv uv sync --frozen --no-dev --extra mcp --python /usr/local/bin/python3.14
WORKDIR /src
# Wheels built from our sources: a plain `pip install ci-common` could resolve a same-named
# package from PyPI (dependency confusion), and an editable install would point at /src.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv build --wheel --package ci-common --out-dir /wheels \
 && uv build --wheel --package advisor --out-dir /wheels \
 && uv pip install --python /venv/bin/python /wheels/ci_common-*.whl \
 && uv pip install --python /venv/bin/python --no-deps /wheels/advisor-*.whl

FROM python:3.14-slim-bookworm
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /venv /venv
COPY --from=build /opt/hermes /opt/hermes
COPY deploy/docker/hermes-config.yaml /opt/hermes-config.yaml
ENV SERVICE=advisor PYTHONPATH=/opt/hermes HERMES_HOME=/tmp/hermes PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
CMD ["/bin/sh", "-c", "mkdir -p \"$HERMES_HOME\" && cp /opt/hermes-config.yaml \"$HERMES_HOME/config.yaml\" && exec /venv/bin/python -m advisor"]
