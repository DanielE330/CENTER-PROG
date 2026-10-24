# CI-hack: cash-flow forecasting service

## Architecture
- Caddy gateway (`infra/docker/Caddyfile`): the only published entry point (`:3000`), serves the SPA, proxies `/api/*` to the Rust API, hides `/internal/*`, sets security headers; automatic TLS when `SITE_ADDRESS=<domain>` and `CADDY_AUTO_HTTPS=on` (also set `COOKIE_SECURE=true`).
- Rust workspace (`backend/`, one Cargo.lock): `api` (BFF, public `:3000` + internal `:3100`, no database), `ledger` (`:3002`, owns `ledger.db`), `forecast` (`:3003`, owns `forecast.db`), `planfact` (`:3004`), shared lib `common`. Internal services listen on internal addresses only; the public contract of `/api/v1/...` is unchanged. See `docs/microservices.md`.
- Python services (`backend/services/`): `auth` (`:8001`), `parser` (`:8002`), `gov` (`:8003`), `ecp` (`:8007`), `integrator` (`:8008`) and the simulators `onec` (`:8004`), `edo` (`:8005`), `bank` (`:8006`), FastAPI/uvicorn.
- Frontend (`web/`, Vite + React) built into the API image and served by the Rust API.
- SQLite is a dev stand-in for PostgreSQL, see `docs/architecture.md`.

## Repository layout
    backend/           Rust workspace: common, api (BFF), ledger, forecast (finance core: src/model.rs, bin eval), planfact
    backend/services/  Python: auth (users, sessions, ECP login), parser (statements), gov (CBR/FNS parsers), tests/
    web/               React + TypeScript SPA
    frontend/          design prototype (mock data)
    infra/             docker/ (Dockerfiles, Caddyfile), docker-compose.yml, Makefile, scripts/ (dev.sh, gen_statement.py)
    docs/              knowledge-base.md (start here), architecture.md, design.md, task/ (case brief PDF), forecast_evaluation.md
    data/              git-ignored: dataset (data/dataset/ from data/raw/*.zip), local run state (data/run/)

## Run without Docker
    infra/scripts/dev.sh    # builds web + Rust workspace, starts all services on http://localhost:3000
    infra/scripts/smoke.sh  # curl smoke test against a running stack (register, import, forecast, tax calendar, sync)
    infra/scripts/dev.sh stop

## Run (Docker only)
    make -C infra up          # docker compose up --build -d
    make -C infra logs
    make -C infra demo-data   # writes data/demo_statement.csv for upload
    make -C infra test        # pytest + cargo test (workspace) in containers
    make -C infra smoke       # smoke test against http://localhost:3000
    make -C infra down

Open http://localhost:3000. Demo login: `demo@example.com` / `demo12345` (seeded when `DEMO_SEED=1`, the default; set `DEMO_SEED=0` to disable).

Ports: only the gateway's `3000` is published. every other service and the internal API `3100` are reachable only inside the compose network. If serving over plain HTTP keep `COOKIE_SECURE=false` (default).
Data lives in named volumes `auth-data`, `ecp-data`, `ledger-data`, `forecast-data`. Docker images: `infra/docker/rust.Dockerfile` (targets ledger, forecast, planfact, api) and `infra/docker/python.Dockerfile` (auth, parser, gov, ecp, onec, edo, bank, integrator).
