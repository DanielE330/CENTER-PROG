# Центр Прогнозирования

Сервис прогноза денежного потока и кассовых разрывов для малого бизнеса (репозиторий `CI-hack`).

## Быстрый старт (нужен только Docker)
    git clone https://github.com/DanielE330/CI-hack.git && cd CI-hack
    ./run.sh            # сборка, запуск всех сервисов и загрузка демо-данных (первая сборка 15–25 мин, дальше быстро)

Откройте http://localhost:3000, демо-вход `demo@example.com` / `demo12345`.
`./run.sh stop` — остановить, `./run.sh reset` — удалить данные, `./run.sh logs`, `./run.sh status`.
Первый оператор платформы задаётся `OPERATOR_EMAIL`/`OPERATOR_PASSWORD` в `infra/.env` (или командой `python -m backoffice.create_operator`), вход на `/operator`.
Порт, HTTPS, данные для имитаторов настраиваются в `infra/.env` (шаблон `infra/.env.example`).
Демо-выписка (компания C001) лежит в репозитории: `data/demo/`. Полный датасет хакатона не нужен: если он есть в `data/dataset/`, укажите `DATASET_DIR=../data/dataset`.
Вход и регистрация по ЭЦП работают в Docker без КриптоПро на сервере: подпись ГОСТ Р 34.10-2012 проверяется на чистом Python (`ECP_VERIFIER=gost`, по умолчанию). Цепочка доверия и отзыв сертификата при этом не проверяются; для этого нужен `ECP_VERIFIER=cryptcp` на машине с КриптоПро. На компьютере пользователя нужны КриптоПро CSP и расширение «КриптоПро ЭЦП Browser plug-in».

## Архитектура
- Шлюз Caddy (`infra/docker/Caddyfile`): единственная публичная точка входа (`:3000`), раздаёт SPA, проксирует `/api/*` в Rust API, скрывает `/internal/*`, выставляет заголовки безопасности; автоматический TLS при `SITE_ADDRESS=<домен>` и `CADDY_AUTO_HTTPS=on` (тогда же задайте `COOKIE_SECURE=true`).
- Rust workspace (`backend/`, один `Cargo.lock`): `api` (BFF, публичный `:3000` + внутренний `:3100`, без базы данных), `ledger` (`:3002`, владеет `ledger.db`), `forecast` (`:3003`, владеет `forecast.db`), `planfact` (`:3004`), общая библиотека `common`. Внутренние сервисы слушают только внутренние адреса; публичный контракт `/api/v1/...` не менялся. Подробнее в `docs/microservices.md`.
- Python-сервисы (`backend/services/`): `auth` (`:8001`), `parser` (`:8002`), `gov` (`:8003`), `ecp` (`:8007`), `integrator` (`:8008`), `notifier` (`:8009`, Telegram-уведомления, цели и консультант Hermes, `docs/notifications.md`), `demo_feed` (`:8012`, лента демо «Прошла неделя»), `llm_gateway` (`:8010`, единственный выход к облачным LLM с маскированием и бюджетом токенов), `backoffice` (`:8013`, панель оператора платформы `/operator`, см. `docs/backoffice.md`), `advisor` (`:8011`, консультант с ИИ; без ключа провайдера отвечает шаблоном из прогноза; включение — `LLM_PROVIDER` и ключ в `infra/.env`) и имитаторы внешних систем `onec` (`:8004`), `edo` (`:8005`), `bank` (`:8006`), FastAPI/uvicorn.
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

Откройте http://localhost:3000. Демо-вход: `demo@example.com` / `demo12345` (создаётся при `DEMO_SEED=1`, это значение по умолчанию; для отключения задайте `DEMO_SEED=0`). У демо-пользователя выписка загружена до 2026-07-12; кнопка «Прошла неделя» на «Обзоре» подаёт следующую неделю через обычный импорт и пересчитывает прогноз (`DEMO_FEED_ENABLED`, `DEMO_CUTOFF`, см. `docs/microservices.md`).

Порты: наружу публикуется только `3000` шлюза. Остальные сервисы и внутренний API `3100` доступны только внутри сети compose. При работе по обычному HTTP оставьте `COOKIE_SECURE=false` (значение по умолчанию).
Данные хранятся в томах `auth-data`, `ecp-data`, `ledger-data`, `forecast-data`, `backoffice-data`, `notifier-data`. Образы Docker: `infra/docker/rust.Dockerfile` (цели ledger, forecast, planfact, api) и `infra/docker/python.Dockerfile` (auth, parser, gov, ecp, onec, edo, bank, integrator, notifier, demo_feed, llm_gateway, advisor, backoffice).

## Установка одной командой

`./install.sh` поднимает сервисы, создаёт оператора с обязательной двухфакторной защитой (TOTP) и печатает адрес, логин и пароль. Подробности и сброс 2FA: [docs/install.md](docs/install.md).

Telegram-уведомления (`notifier`): алерт кассового разрыва за 7/3/1 день, недельный риск-отчёт, итоги недели и месяца с планом, agile-цели, Excel-отчёт, консультант Hermes (`HERMES_URL`), роли и политики отделов, команда компании с ролями (`docs/team.md`), ML-лаборатория и единый бэктест (`ml/`, `docs/ml-architecture.md`). Токен бота — `TELEGRAM_BOT_TOKEN` в окружении.
