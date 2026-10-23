# CI-hack: cash-flow forecasting service

## Architecture
- Caddy gateway (`docker/Caddyfile`): the only published entry point (`:3000`), serves the SPA, proxies `/api/*` to the Rust API, hides `/internal/*`, sets security headers; automatic TLS when `SITE_ADDRESS=<domain>` and `CADDY_AUTO_HTTPS=on` (also set `COOKIE_SECURE=true`).
- Rust API (`crates/api`): public API on `:3000` and internal API on `:3100`, both inside the compose network only (the dev run without Docker also serves the SPA directly).
- Python services (`services/`): `auth` (`:8001`) and `parser` (`:8002`), FastAPI/uvicorn.
- Frontend (`web/`, Vite + React) built into the API image and served by the Rust API.
- SQLite is a dev stand-in for PostgreSQL, see `docs/architecture.md`.

## Repository layout
    crates/api/        Rust: public + internal API, finance core (src/forecast.rs)
    services/auth/     Python: users, sessions
    services/parser/   Python: statement parser + categorisation
    services/tests/    pytest for the Python services
    web/               React + TypeScript SPA
    docker/            Dockerfiles, Caddyfile
    docker-compose.yml, Makefile
    scripts/           dev.sh (run without Docker), gen_statement.py (demo data)
    docs/              architecture.md, design.md, task/ (case brief PDF), forecast_evaluation.md
    data/              git-ignored: dataset (data/dataset/ from data/raw/*.zip), local run state (data/run/)

## Run without Docker
    scripts/dev.sh          # builds web + api, starts auth, parser, api on http://localhost:3000
    scripts/dev.sh stop

## Run (Docker only)
    make up          # docker compose up --build -d
    make logs
    make demo-data   # writes data/demo_statement.csv for upload
    make test        # pytest + cargo test in containers
    make down

Open http://localhost:3000. Demo login: `demo@example.com` / `demo12345` (seeded when `DEMO_SEED=1`, the default; set `DEMO_SEED=0` to disable).

Ports: only the gateway's `3000` is published. `auth`, `parser` and the internal API `3100` are reachable only inside the compose network. If serving over plain HTTP keep `COOKIE_SECURE=false` (default).
Data lives in named volumes `auth-data` and `api-data`.
