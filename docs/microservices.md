# Микросервисы: один сервис — одна задача

Целевая раскладка (папки в `backend/`, инфраструктура в `infra/`). Сервисы общаются только по HTTP внутри сети; БД есть только у владельца данных.

| Сервис | Язык, порт | Единственная задача | Владеет данными |
|---|---|---|---|
| gateway | Caddy, 3000 | Входная точка: TLS, заголовки, раздача SPA, маршрутизация `/api/*` | — |
| api | Rust, 3000 (+ internal 3100) | Публичный API для SPA (BFF): сессия через auth, CSRF, заголовки, раздача SPA, проксирование и оркестрация (налоговый календарь, sync интеграций, вход по ЭЦП, ключевая ставка) | — |
| auth | Python, 8001 | Пароли, пользователи, серверные сессии | `auth.db` |
| ecp | Python, 8007 | Вход по ЭЦП: challenge, проверка подписи CAdES, привязка сертификата к пользователю | `ecp.db` |
| parser | Python, 8002 | Разбор файла выписки (CSV/XLSX) и категоризация операций | — |
| ledger | Rust, 3002 | Компании, импорты (staging + вызов parser), операции, ожидаемые события, `data_revision`, аудит | `ledger.db` |
| forecast | Rust, 3003 | Расчёт прогноза остатка, риска разрыва, сценариев и backtest; авто-пересчёт устаревшего базового прогноза | `forecast.db` (`forecast_runs`) |
| planfact | Rust, 3004 | Сравнение плана и факта (история и события из ledger, регулярные платежи из forecast) | — |
| gov | Python, 8003 | Получение публичных данных госсайтов: ключевая ставка ЦБ, календари, налоговый календарь ФНС | кэш |
| integrator | Python, 8008 | Забор счетов, договоров и документов из 1С, ЭДО и банка и приведение к ожидаемым событиям | — |
| onec | Python, 8004 | Имитация 1С (OData) | — |
| edo | Python, 8005 | Имитация ЭДО | — |
| bank | Python, 8006 | Имитация банковского API выписок | — |

Принципы: сервис не читает чужую БД; общие структуры данных (операция, событие) лежат в общем крейте/схеме, а не копируются; каждый сервис имеет `/live`, свои тесты, свой Dockerfile target и запись в `infra/docker-compose.yml`.

## Rust-сервисы: код и внутренние API

Cargo workspace в `backend/` (общие `Cargo.lock`, `rust-toolchain.toml`): `common` (lib), `api`, `ledger`, `forecast` (lib `fc_model` + bin `forecast` + bin `eval`), `planfact`. `common` содержит только общие структуры (`Tx`, `Event`, `Snapshot`), перевод денег в строки, тип ошибки API (`{"error":{"code","message"}}`) и клиент межсервисных вызовов `Upstream` (таймауты, 503 `<сервис>_unavailable` при недоступности, проброс `x-request-id` и `x-actor`).

| Сервис | Env (по умолчанию) | Эндпоинты `/internal/v1/...` |
|---|---|---|
| ledger :3002 | `INTERNAL_ADDR=127.0.0.1:3002`, `DB_PATH=data/run/ledger.db`, `PARSER_URL` | `companies/ensure`, `companies/{cid}` (GET, PATCH), `companies/{cid}/snapshot`, `.../imports` (GET, POST multipart), `.../imports/{iid}` (+`/confirm`), `.../transactions`, `.../transactions/{id}/category`, `.../expected-events` (GET, POST), `.../expected-events/{eid}` (DELETE), `.../expected-events/bulk` (идемпотентно по `external_id`), `.../audit`, `health`, `/live` |
| forecast :3003 | `INTERNAL_ADDR=127.0.0.1:3003`, `DB_PATH=data/run/forecast.db`, `LEDGER_URL` | `companies/{cid}/forecasts` (POST), `.../forecasts/latest`, `.../forecasts/{rid}`, `.../scenarios`, `.../backtest`, `.../summary`, `.../rules?as_of=` |
| planfact :3004 | `INTERNAL_ADDR=127.0.0.1:3004`, `LEDGER_URL`, `FORECAST_URL` | `companies/{cid}/plan-fact?days=` |
| api :3000/:3100 | `PUBLIC_ADDR`, `INTERNAL_ADDR`, `AUTH_URL`, `ECP_URL`, `GOV_URL`, `LEDGER_URL`, `FORECAST_URL`, `PLANFACT_URL`, `INTEGRATOR_URL`, `WEB_DIR`, `COOKIE_SECURE` | публичные `/api/v1/...` (контракт для SPA не менялся); внутренний :3100: `health`, `companies/{cid}/summary` |

Внутренние сервисы доверяют api: проверка сессии и принадлежности компании выполняется только в api (чужая компания -> 404), ответы ledger/forecast/planfact передаются в SPA без изменений. Сервисы ledger/forecast/planfact слушают только адреса из env (в Docker — только внутренняя сеть); Caddy отдаёт 404 на `/internal/*`.

Запуск: `infra/scripts/dev.sh` (собирает workspace, поднимает всё; `dev.sh stop`). Проверка: `cd backend && cargo test`, `infra/scripts/smoke.sh`. Docker: `make -C infra up`; образы — `infra/docker/rust.Dockerfile` (targets `ledger`, `forecast`, `planfact`, `api`) и `infra/docker/python.Dockerfile` (`auth`, `parser`, `gov`, `ecp`, `onec`, `edo`, `bank`, `integrator`). Сеть `edge` у gateway, api и gov; имитаторы, integrator и все Rust-сервисы кроме api — только `internal`.

`POST /api/v1/companies/{cid}/integrations/sync` `{"external_company":"C001"}`: api запрашивает у integrator `GET /internal/v1/obligations?company=...` (`INTEGRATOR_URL`, `http://127.0.0.1:8008`), превращает обязательства в ожидаемые события (`source`: `contract` для kind=contract, иначе `invoice`) и записывает их в ledger через `expected-events/bulk`; ключ идемпотентности — уникальный индекс `(company_id, external_id)`. Ответ `{created, skipped, sources}`.
