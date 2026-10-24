# CI-hack: cash-flow forecasting service

## Architecture
- Caddy gateway (`infra/docker/Caddyfile`): the only published entry point (`:3000`), serves the SPA, proxies `/api/*` to the Rust API, hides `/internal/*`, sets security headers; automatic TLS when `SITE_ADDRESS=<domain>` and `CADDY_AUTO_HTTPS=on` (also set `COOKIE_SECURE=true`).
- Rust API (`backend/api`): public API on `:3000` and internal API on `:3100`, both inside the compose network only (the dev run without Docker also serves the SPA directly).
- Python services (`backend/services/`): `auth` (`:8001`), `parser` (`:8002`) and `gov` (`:8003`, gov-site parsers), FastAPI/uvicorn.
- Frontend (`web/`, Vite + React) built into the API image and served by the Rust API.
- SQLite is a dev stand-in for PostgreSQL, see `docs/architecture.md`.

## Repository layout
    backend/api/       Rust: public + internal API, finance core (src/forecast.rs)
    backend/services/  Python: auth (users, sessions, ECP login), parser (statements), gov (CBR/FNS parsers), tests/
    web/               React + TypeScript SPA
    frontend/          design prototype (mock data)
    infra/             docker/ (Dockerfiles, Caddyfile), docker-compose.yml, Makefile, scripts/ (dev.sh, gen_statement.py)
    docs/              knowledge-base.md (start here), architecture.md, design.md, task/ (case brief PDF), forecast_evaluation.md
    data/              git-ignored: dataset (data/dataset/ from data/raw/*.zip), local run state (data/run/)

## Run without Docker
    infra/scripts/dev.sh    # builds web + api, starts auth, parser, gov, api on http://localhost:3000
    infra/scripts/dev.sh stop

## Run (Docker only)
    make -C infra up          # docker compose up --build -d
    make -C infra logs
    make -C infra demo-data   # writes data/demo_statement.csv for upload
    make -C infra test        # pytest + cargo test in containers
    make -C infra down

Open http://localhost:3000. Demo login: `demo@example.com` / `demo12345` (seeded when `DEMO_SEED=1`, the default; set `DEMO_SEED=0` to disable).

Ports: only the gateway's `3000` is published. `auth`, `parser` and the internal API `3100` are reachable only inside the compose network. If serving over plain HTTP keep `COOKIE_SECURE=false` (default).
Data lives in named volumes `auth-data` and `api-data`.
