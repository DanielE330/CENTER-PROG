# Микросервисы: один сервис — одна задача

Целевая раскладка (папки в `backend/`, инфраструктура в `infra/`). Сервисы общаются только по HTTP внутри сети; БД есть только у владельца данных.

| Сервис | Язык, порт | Единственная задача | Владеет данными |
|---|---|---|---|
| gateway | Caddy, 3000 | Входная точка: TLS, заголовки, раздача SPA, маршрутизация `/api/*` | — |
| api | Rust, 3000 (+ internal 3100) | Публичный API для SPA (BFF): сессия через auth, CSRF, заголовки, раздача SPA, проксирование и оркестрация (налоговый календарь, sync интеграций, вход по ЭЦП, ключевая ставка) | — |
| auth | Python, 8001 | Пароли, пользователи, серверные сессии, API-токены, сотрудники (создание с заданным или сгенерированным паролем, смена и сброс пароля, закрытие сессий, удаление) | `auth.db` |
| ecp | Python, 8007 | Вход по ЭЦП: challenge, проверка подписи CAdES, привязка сертификата к пользователю; директор по ЭЦП создаётся без пароля (пароль обязателен, см. [roles.md](roles.md)) | `ecp.db` |
| parser | Python, 8002 | Разбор файла выписки (CSV/XLSX) и категоризация операций | — |
| ledger | Rust, 3002 | Компании, участники и роли (`company_members`), блокировка компании оператором, метаданные и агрегаты для оператора (`admin/*`), импорты (staging + вызов parser), операции, ожидаемые события, `data_revision`, аудит | `ledger.db` |
| forecast | Rust, 3003 | Расчёт прогноза остатка, риска разрыва, сценариев и backtest; авто-пересчёт устаревшего базового прогноза | `forecast.db` (`forecast_runs`) |
| planfact | Rust, 3004 | Сравнение плана и факта (история и события из ledger, регулярные платежи из forecast) | — |
| gov | Python, 8003 | Получение публичных данных госсайтов: ключевая ставка ЦБ, календари, налоговый календарь ФНС | кэш |
| integrator | Python, 8008 | Забор счетов, договоров и документов из 1С, ЭДО и банка и приведение к ожидаемым событиям | — |
| llm_gateway | Python, 8010 | Единственный выход к облачным LLM (Anthropic, OpenAI, Gemini, OpenRouter через httpx): маскирование ИНН/счетов/карт/контрагентов/ФИО перед отправкой и обратная подстановка в ответе, суточный бюджет токенов на пользователя, аудит обращений без содержимого, таймауты | `llm_gateway.db` (расход токенов, аудит) |
| advisor | Python, 8011 | Консультант: агентный цикл с инструментами (прогноз и риск, ближайшие платежи и просрочки, план-факт, сценарий «что если», операции по категориям) поверх forecast/ledger/planfact и llm_gateway; только предлагает действия; без провайдера отвечает шаблоном из прогноза | — |
| demo_feed | Python, 8012 | Лента демо «Прошла неделя»: по курсору (дате последней операции) отдаёт операции следующей недели демо-компании из датасета | — |
| backoffice | Python, 8013 | Панель оператора платформы: операторы, сессии, журнал действий; список компаний (только метаданные и агрегаты), статистика, блокировка/разблокировка компании с отзывом сессий участников; прокси к ledger/forecast/auth. См. `docs/backoffice.md` | `backoffice.db` (операторы, сессии, аудит) |
| onec | Python, 8004 | Имитация 1С (OData) | — |
| edo | Python, 8005 | Имитация ЭДО | — |
| bank | Python, 8006 | Имитация банковского API выписок | — |

Принципы: сервис не читает чужую БД; общие структуры данных (операция, событие) лежат в общем крейте/схеме, а не копируются; каждый сервис имеет `/live`, свои тесты, свой Dockerfile target и запись в `infra/docker-compose.yml`.

## Rust-сервисы: код и внутренние API

Cargo workspace в `backend/` (общие `Cargo.lock`, `rust-toolchain.toml`): `common` (lib), `api`, `ledger`, `forecast` (lib `fc_model` + bin `forecast` + bin `eval`), `planfact`. `common` содержит только общие структуры (`Tx`, `Event`, `Snapshot`), перевод денег в строки, тип ошибки API (`{"error":{"code","message"}}`) и клиент межсервисных вызовов `Upstream` (таймауты, 503 `<сервис>_unavailable` при недоступности, проброс `x-request-id` и `x-actor`).

| Сервис | Env (по умолчанию) | Эндпоинты `/internal/v1/...` |
|---|---|---|
| ledger :3002 | `INTERNAL_ADDR=127.0.0.1:3002`, `DB_PATH=data/run/ledger.db`, `PARSER_URL` | `companies/ensure`, `members/by-user/{uid}`, `companies/{cid}/members` (GET, POST), `.../members/{uid}` (PATCH, DELETE), `admin/companies` (GET), `admin/companies/{cid}` (GET), `.../ban`, `.../unban`, `admin/stats`, `companies/{cid}` (GET, PATCH), `companies/{cid}/snapshot`, `.../imports` (GET, POST multipart), `.../imports/{iid}` (+`/confirm`), `.../transactions`, `.../transactions/{id}/category`, `.../expected-events` (GET, POST), `.../expected-events/{eid}` (DELETE), `.../expected-events/bulk` (идемпотентно по `external_id`), `.../audit`, `health`, `/live` |
| forecast :3003 | `INTERNAL_ADDR=127.0.0.1:3003`, `DB_PATH=data/run/forecast.db`, `LEDGER_URL` | `companies/{cid}/forecasts` (POST), `.../forecasts/latest`, `.../forecasts/{rid}`, `.../scenarios`, `.../backtest`, `.../summary`, `.../rules?as_of=` |
| planfact :3004 | `INTERNAL_ADDR=127.0.0.1:3004`, `LEDGER_URL`, `FORECAST_URL` | `companies/{cid}/plan-fact?days=` |
| api :3000/:3100 | `PUBLIC_ADDR`, `INTERNAL_ADDR`, `AUTH_URL`, `BACKOFFICE_URL`, `ECP_URL`, `GOV_URL`, `LEDGER_URL`, `FORECAST_URL`, `PLANFACT_URL`, `INTEGRATOR_URL`, `WEB_DIR`, `COOKIE_SECURE` | публичные `/api/v1/...` (контракт для SPA не менялся), операторские `/api/admin/v1/...` (`admin.rs`, cookie `op_session`); внутренний :3100: `health`, `companies/{cid}/summary` |

Внутренние сервисы доверяют api: проверка сессии, роли и прав (`read`/`edit`/`manage`, 403 `forbidden`), блокировки компании (403 `company_banned`) и принадлежности компании выполняется только в api (чужая компания -> 404; модель ролей и таблица «маршрут -> право» — [roles.md](roles.md)), ответы ledger/forecast/planfact передаются в SPA без изменений. Сервисы ledger/forecast/planfact слушают только адреса из env (в Docker — только внутренняя сеть); Caddy отдаёт 404 на `/internal/*`.

Запуск: `infra/scripts/dev.sh` (собирает workspace, поднимает всё; `dev.sh stop`). Проверка: `cd backend && cargo test`, `infra/scripts/smoke.sh`. Docker: `make -C infra up`; образы — `infra/docker/rust.Dockerfile` (targets `ledger`, `forecast`, `planfact`, `api`) и `infra/docker/python.Dockerfile` (`auth`, `parser`, `gov`, `ecp`, `onec`, `edo`, `bank`, `integrator`, `demo_feed`, `llm_gateway`, `advisor`, `backoffice`). Сеть `edge` у gateway, api, gov и llm_gateway (выход к провайдерам); имитаторы, integrator и все Rust-сервисы кроме api — только `internal`.

`POST /api/v1/companies/{cid}/integrations/sync` `{"external_company":"C001"}`: api запрашивает у integrator `GET /internal/v1/obligations?company=...` (`INTEGRATOR_URL`, `http://127.0.0.1:8008`), превращает обязательства в ожидаемые события (`source`: `contract` для kind=contract, иначе `invoice`) и записывает их в ledger через `expected-events/bulk`; ключ идемпотентности — уникальный индекс `(company_id, external_id)`. Ответ `{created, skipped, sources}`.

## Демо «Прошла неделя»

У демо-пользователя `demo@example.com` «сегодня» — дата отсечки внутри выписки: `seed_demo.sh` грузит C001 только до `DEMO_CUTOFF` (по умолчанию 2026-07-12; после неё 131 операция, около 13 недель), остальное лежит «в будущем» и отдаётся сервисом `demo_feed` (:8012; env `DEMO_DATASET_DIR`/`MOCK_DATASET_DIR`, `DEMO_CUTOFF`, `DEMO_COMPANY`; для `seed_demo.sh` и `demo_feed` нужен один и тот же датасет). `demo_feed`: `/live`, `/internal/v1/feed/status?company=&after=`, `/internal/v1/feed/week?company=&after=` (JSON: `from`, `to`, `count`, `weeks_left`, `filename`, `csv`) и `/internal/v1/feed/week.csv`. Курсор `after` — дата последней операции в ledger: лента отдаёт следующие 7 дней (пустые недели пропускаются), CSV той же структуры, что и загружаемая выписка; когда данные кончились — 409 `feed_exhausted`.

api (`backend/api/src/demo.rs`, `DEMO_FEED_URL`, `DEMO_FEED_ENABLED=1`, `DEMO_EMAIL`, `DEMO_COMPANY`):
- `GET /api/v1/companies/{cid}/demo/status` -> `{enabled:false}` для всех, кроме демо-компании; для демо `{enabled, available, as_of, cutoff, next_from, weeks_left, ops_left, finished}`.
- `POST /api/v1/companies/{cid}/demo/advance-week` -> 201 `{added, from, to, new_as_of, previous_as_of, rows, skipped_duplicates, weeks_left, import_id, recalc_ms, forecast_vs_fact}`. Берёт неделю у ленты, прогоняет через обычный импорт ledger (upload, затем confirm), затем читает `forecasts/latest` (forecast сам пересчитывает устаревший базовый прогноз, это время и есть `recalc_ms`). `forecast_vs_fact` — прежний прогноз (P10/P50/P90) на `new_as_of` против фактического остатка. Параллельные вызовы выполняются по очереди. Ошибки: 403 `demo_disabled` (не демо-компания или флаг выключен), 404 (чужая компания), 409 `demo_not_seeded` / `feed_exhausted`, 503 `demo_feed_unavailable`.
- Демо-компания: у ledger нет признака «демо», поэтому это компания пользователя с e-mail `DEMO_EMAIL` (по умолчанию `demo@example.com`) при `DEMO_FEED_ENABLED=1`; в compose флаг по умолчанию равен `DEMO_SEED`.

В интерфейсе плашка на «Обзоре» (`DemoWeekCard`) видна только при `demo/status.enabled`; после кнопки обновляются все запросы (TanStack Query), на «Плане и факте» виден новый отрезок. Чтобы пройти демо заново: `./run.sh reset && ./run.sh` (или `DEMO_CUTOFF=none` для выписки целиком). Имитаторы 1С/ЭДО/банка по-прежнему живут на `MOCK_TODAY`.

## Консультант с ИИ: llm_gateway (:8010) и advisor (:8011)

Публичный API (api, `backend/api/src/advisor.rs`, сессия и принадлежность компании проверяет api):
- `POST /api/v1/companies/{cid}/advisor/ask` `{question, history?:[{role,content}], lang?:"ru"|"en"}` -> `{answer, suggestions:[{id,type,params,title}], provider, model, notice, usage, tools_used}`. `provider:"none"` — ответ по шаблону из прогноза (дата и сумма ближайшего разрыва, причины, три действия); `notice`: `provider_not_configured` | `provider_unavailable` | `budget_exceeded`.
- `POST /api/v1/companies/{cid}/advisor/suggestions/apply` `{type:"add_expected_payment", params:{direction:"in"|"out", amount_kop, date, title, counterparty?}}` -> 201 `{id,type}`: api заново проверяет параметры и создаёт ожидаемое событие в ledger (`source: manual`). Консультант сам ничего не записывает.

Внутренние: `advisor` `POST /internal/v1/companies/{cid}/ask` (env `LLM_GATEWAY_URL`, `LEDGER_URL`, `FORECAST_URL`, `PLANFACT_URL`, `ADVISOR_TIMEOUT_SECONDS`=75); `llm_gateway` `POST /internal/v1/chat {user,messages,tools?,protect?,max_tokens?}`, `GET /internal/v1/status`, `GET /internal/v1/audit`. Ошибки шлюза: `provider_not_configured` (503), `budget_exceeded` (429), `provider_unavailable` (503/502). Env шлюза: `LLM_PROVIDER` (anthropic|openai|gemini|openrouter), `LLM_MODEL`, ключи `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`GEMINI_API_KEY`/`OPENROUTER_API_KEY` (только из окружения, в репозитории их нет), `LLM_DAILY_TOKEN_BUDGET` (по умолчанию 200000, 0 — без лимита), `LLM_TIMEOUT_SECONDS`, `LLM_GATEWAY_DB`. Включение: задать `LLM_PROVIDER` и ключ в `infra/.env` (или в окружении) и перезапустить `llm-gateway`.
