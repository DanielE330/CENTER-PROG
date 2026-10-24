# CI-hack: сервис прогноза денежного потока

## Быстрый старт (нужен только Docker)
    git clone https://github.com/DanielE330/CI-hack.git && cd CI-hack
    ./run.sh            # сборка, запуск всех сервисов и загрузка демо-данных (первая сборка 15–25 мин, дальше быстро)

Откройте http://localhost:3000, демо-вход `demo@example.com` / `demo12345`.
`./run.sh stop` — остановить, `./run.sh reset` — удалить данные, `./run.sh logs`, `./run.sh status`.
Порт, HTTPS, данные для имитаторов настраиваются в `infra/.env` (шаблон `infra/.env.example`).
Демо-выписка (компания C001) лежит в репозитории: `data/demo/`. Полный датасет хакатона не нужен: если он есть в `data/dataset/`, укажите `DATASET_DIR=../data/dataset`.
Вход по ЭЦП в Docker не проверяет подпись: в образ `ecp` нельзя включить проприетарный КриптоПро CSP. Для проверки подписи запускайте стек без Docker (`infra/scripts/dev.sh`) на машине с КриптоПро.

## Архитектура
- Шлюз Caddy (`infra/docker/Caddyfile`): единственная публичная точка входа (`:3000`), раздаёт SPA, проксирует `/api/*` в Rust API, скрывает `/internal/*`, выставляет заголовки безопасности; автоматический TLS при `SITE_ADDRESS=<домен>` и `CADDY_AUTO_HTTPS=on` (тогда же задайте `COOKIE_SECURE=true`).
- Rust workspace (`backend/`, один `Cargo.lock`): `api` (BFF, публичный `:3000` + внутренний `:3100`, без базы данных), `ledger` (`:3002`, владеет `ledger.db`), `forecast` (`:3003`, владеет `forecast.db`), `planfact` (`:3004`), общая библиотека `common`. Внутренние сервисы слушают только внутренние адреса; публичный контракт `/api/v1/...` не менялся. Подробнее в `docs/microservices.md`.
- Python-сервисы (`backend/services/`): `auth` (`:8001`), `parser` (`:8002`), `gov` (`:8003`), `ecp` (`:8007`), `integrator` (`:8008`) и имитаторы внешних систем `onec` (`:8004`), `edo` (`:8005`), `bank` (`:8006`), FastAPI/uvicorn.
- Фронтенд (`frontend/`, Vite + React + Tailwind) собирается в образ API и раздаётся Rust API.
- SQLite — временная замена PostgreSQL на время разработки, см. `docs/architecture.md`.

## Структура репозитория
    backend/           Rust workspace: common, api (BFF), ledger, forecast (финансовое ядро: src/model.rs, bin eval), planfact
    backend/services/  Python: auth (пользователи, сессии), ecp (вход по ЭЦП), parser (выписки), gov (парсеры ЦБ и ФНС),
                       integrator, onec/edo/bank (имитаторы), tests/
    frontend/          React + TypeScript SPA (Vite, Tailwind, TanStack Query, i18n RU/EN, тёмная тема)
    infra/             docker/ (Dockerfile, Caddyfile), docker-compose.yml, Makefile, scripts/ (dev.sh, smoke.sh, gen_statement.py)
    docs/              knowledge-base.md (начните отсюда), architecture.md, design.md, microservices.md, task/ (описание кейса, PDF), forecast_evaluation.md
    data/              в .gitignore: датасет (data/dataset/ из data/raw/*.zip), состояние локального запуска (data/run/)

## Запуск без Docker
    infra/scripts/dev.sh    # собирает frontend и Rust workspace, поднимает все сервисы на http://localhost:3000
    infra/scripts/smoke.sh  # сквозной тест через curl на запущенном стеке (регистрация, импорт, прогноз, налоговый календарь, sync)
    infra/scripts/seed_demo.sh  # загружает выписку C001 демо-пользователю (demo@example.com) и строит прогноз
    infra/scripts/dev.sh stop

## Запуск (только Docker)
    make -C infra up          # docker compose up --build -d
    make -C infra logs
    make -C infra demo-data   # создаёт data/demo_statement.csv для загрузки
    make -C infra test        # pytest + cargo test (workspace) в контейнерах
    make -C infra smoke       # сквозной тест на http://localhost:3000
    make -C infra seed        # демо-данные для demo@example.com (стек должен быть запущен)
    make -C infra down

Откройте http://localhost:3000. Демо-вход: `demo@example.com` / `demo12345` (создаётся при `DEMO_SEED=1`, это значение по умолчанию; для отключения задайте `DEMO_SEED=0`).

Порты: наружу публикуется только `3000` шлюза. Остальные сервисы и внутренний API `3100` доступны только внутри сети compose. При работе по обычному HTTP оставьте `COOKIE_SECURE=false` (значение по умолчанию).
Данные хранятся в томах `auth-data`, `ecp-data`, `ledger-data`, `forecast-data`. Образы Docker: `infra/docker/rust.Dockerfile` (цели ledger, forecast, planfact, api) и `infra/docker/python.Dockerfile` (auth, parser, gov, ecp, onec, edo, bank, integrator).
